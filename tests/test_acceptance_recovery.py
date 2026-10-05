import pytest
import sqlite3
import threading

from .conftest import setup_project_with_data, start_run
from .test_review_plan import _complete_run_with_kept
from prompt_lib import runs
from prompt_lib.core import BizError
from prompt_lib.db import DB, get_db


@pytest.mark.parametrize("interruption", ["after_binding", "third_output", "before_report", "before_completion"])
def test_acceptance_recovers_bound_manifest_and_completed_calls(client, monkeypatch, interruption):
    data = setup_project_with_data(client)
    run, _ = _complete_run_with_kept(client, data)
    db = get_db()
    actual_generate = runs.generate_once
    calls = 0
    interrupted = False

    def generate(*args, **kwargs):
        nonlocal calls, interrupted
        if interruption == "after_binding" and not interrupted:
            interrupted = True
            raise OSError("interrupted after atomic binding, before dispatch")
        result = actual_generate(*args, **kwargs)
        calls += 1
        if interruption == "third_output" and calls == 3:
            interrupted = True
            raise BizError("BUDGET_EXHAUSTED", "interrupted after saved third output")
        return result

    monkeypatch.setattr(runs, "generate_once", generate)
    if interruption == "before_report":
        db.execute("CREATE TEMP TRIGGER interrupt_report BEFORE INSERT ON acceptance_reports "
                   "BEGIN SELECT RAISE(ABORT, 'interrupted before report insert'); END")
        interrupted = True
    if interruption == "before_completion":
        db.execute("CREATE TEMP TRIGGER interrupt_report BEFORE UPDATE ON acceptance_jobs "
                   "WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT, 'interrupted before job completion'); END")
        interrupted = True
    with pytest.raises((BizError, OSError, sqlite3.DatabaseError)):
        runs.AcceptanceService(db).accept(run["id"])
    assert interrupted
    job = dict(db.one("SELECT * FROM acceptance_jobs WHERE run_id=?", (run["id"],)))
    assert job["report_id"] == ""
    assert db.one("SELECT COUNT(*) n FROM acceptance_reports WHERE run_id=?", (run["id"],))["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'",
                  (data["pid"],))["n"] == 0
    prior_ids = {row["id"] for row in db.query("SELECT id FROM outputs WHERE run_id=? AND item_id IN (SELECT item_id FROM sealed_artifacts)", (run["id"],))}
    assert len(prior_ids) == {"after_binding": 0, "third_output": 3, "before_report": 4, "before_completion": 4}[interruption]
    prior_count = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=? AND phase='acceptance'", (run["id"],))["n"]
    monkeypatch.setattr(runs, "generate_once", actual_generate)
    if interruption in ("before_report", "before_completion"):
        db.execute("DROP TRIGGER interrupt_report")
    reopened = DB(db.path)
    try:
        report = runs.AcceptanceService(reopened).accept(run["id"])
        repeated = runs.AcceptanceService(reopened).accept(run["id"])
        assert report["acceptance_id"] == job["id"]
        assert repeated["id"] == report["id"]
        outputs = reopened.query("SELECT id FROM outputs WHERE run_id=? AND item_id IN (SELECT item_id FROM sealed_artifacts)", (run["id"],))
        assert len(outputs) == 4
        assert prior_ids <= {row["id"] for row in outputs}
        final_count = reopened.one("SELECT COUNT(*) n FROM ledger WHERE run_id=? AND phase='acceptance'", (run["id"],))["n"]
        assert final_count == 8
        if interruption in ("before_report", "before_completion"):
            assert final_count == prior_count
        assert reopened.one("SELECT COUNT(*) n FROM acceptance_reports WHERE run_id=?", (run["id"],))["n"] == 1
        assert reopened.one("SELECT manifest_hash,report_id FROM acceptance_jobs WHERE id=?", (job["id"],))["report_id"] == report["id"]
    finally:
        reopened._conn.close()


def test_exam_budget_pause_increase_and_resume_via_api(client):
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0,
                    budget={"mode": "token", "total_limit": 1000000,
                            "search_limit": 900000, "acceptance_limit": 500})
    assert run["state"] == "completed"
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={"candidate_id": "baseline"}).status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "BUDGET_EXHAUSTED"
    job = client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-job").json()["job"]
    assert job["state"] == "paused_budget"
    assert job["case_count"] == 2
    assert not ({"manifest_json", "runtime_input", "evaluation_only"} & set(job))
    updated = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget", json={
        "revision": run["revision"], "limits": {"total_limit": 1100000,
        "search_limit": 900000, "acceptance_limit": 100000}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["snapshot_hash"] == run["snapshot_hash"]
    report = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert report.status_code == 202, report.text
    completed = client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-job").json()["job"]
    assert completed["id"] == job["id"]
    assert completed["state"] == "completed"
    assert completed["report_id"] == report.json()["id"]
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/acceptance-job/cancel").status_code == 409


def test_startup_recovers_running_exam_without_reopening_consumed_cases(client):
    data = setup_project_with_data(client)
    run, _ = _complete_run_with_kept(client, data)
    from prompt_lib.acceptance_jobs import bind_exam
    from prompt_lib.domain import PromptService, ProjectsService
    db = get_db()
    baseline = PromptService(db).get(run["baseline_prompt_id"])
    job = bind_exam(db, runs.RunService(db).get(run["id"]), baseline, baseline,
                    ProjectsService(db).get(data["pid"])["contract"])
    db.execute("UPDATE acceptance_jobs SET state='running' WHERE id=?", (job["id"],))
    runs.RunService(db).recover_interrupted()
    recovered = runs.AcceptanceService(db).job(run["id"])
    assert recovered["state"] == "paused_interrupted"
    assert recovered["id"] == job["id"]
    db.execute("UPDATE acceptance_jobs SET state='cancelling' WHERE id=?", (job["id"],))
    runs.RunService(db).recover_interrupted()
    assert runs.AcceptanceService(db).job(run["id"])["state"] == "cancelled"
    assert db.one("SELECT COUNT(*) n FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'",
                  (data["pid"],))["n"] == 0


def test_cancel_during_generation_preserves_output_and_prevents_next_request(client, monkeypatch):
    data = setup_project_with_data(client)
    run, _ = _complete_run_with_kept(client, data)
    db = get_db()
    actual_generate = runs.generate_once
    output_saved, release = threading.Event(), threading.Event()
    errors = []

    def generate(*args, **kwargs):
        output = actual_generate(*args, **kwargs)
        output_saved.set()
        assert release.wait(10)
        return output

    def execute_exam():
        try:
            runs.AcceptanceService(db).accept(run["id"])
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(runs, "generate_once", generate)
    worker = threading.Thread(target=execute_exam)
    worker.start()
    try:
        assert output_saved.wait(10)
        result = client.post(f"/workflow-api/v1/runs/{run['id']}/acceptance-job/cancel")
        assert result.status_code == 200, result.text
        assert result.json()["job"]["state"] == "cancelling"
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], BizError)
    assert errors[0].code == "EXAM_CANCELLED"
    assert runs.AcceptanceService(db).job(run["id"])["state"] == "cancelled"
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=? AND phase='acceptance'", (run["id"],))["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM outputs WHERE run_id=? AND item_id IN (SELECT item_id FROM sealed_artifacts)", (run["id"],))["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM acceptance_reports WHERE run_id=?", (run["id"],))["n"] == 0
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/accept").status_code == 409
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/acceptance-job/cancel").json()["job"]["state"] == "cancelled"
    assert db.one("SELECT COUNT(*) n FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'", (data["pid"],))["n"] == 0

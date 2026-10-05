import pytest

from .conftest import setup_project_with_data, start_run, wait_run
from prompt_lib.core import BizError
from prompt_lib.db import get_db
from prompt_lib import runs


@pytest.mark.parametrize("interruption", ["during_evaluation", "before_progress_commit"])
def test_round_resume_keeps_candidate_and_does_not_repeat_rewrite(client, monkeypatch, interruption):
    data = setup_project_with_data(client)
    interrupted = False
    actual_score = runs.score_prompt
    actual_progress = runs.RunService._persist_progress
    def score(db, project, prompt, ids, *args, **kwargs):
        nonlocal interrupted
        if interruption == "during_evaluation" and prompt["origin"] == "optimizer" and not interrupted:
            interrupted = True
            actual_score(db, project, prompt, ids[:1], *args, **kwargs)
            raise BizError("BUDGET_EXHAUSTED", "test interruption after first candidate result")
        return actual_score(db, project, prompt, ids, *args, **kwargs)
    def progress(self, rid, round_no, *args, **kwargs):
        nonlocal interrupted
        if interruption == "before_progress_commit" and round_no == 1 and not interrupted:
            interrupted = True
            raise BizError("BUDGET_EXHAUSTED", "test interruption after scored checkpoint")
        return actual_progress(self, rid, round_no, *args, **kwargs)
    monkeypatch.setattr(runs, "score_prompt", score)
    monkeypatch.setattr(runs.RunService, "_persist_progress", progress)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:4], max_candidates=1)
    assert run["state"] == "paused_budget", run
    assert interrupted
    db = get_db()
    checkpoint = db.one("SELECT prompt_version_id,status FROM run_rounds WHERE run_id=? AND round_no=1", (run["id"],))
    candidate = checkpoint["prompt_version_id"]
    assert checkpoint["status"] == ("candidate_created" if interruption == "during_evaluation" else "scored")
    prior_outputs = {row["id"] for row in db.query("SELECT id FROM outputs WHERE run_id=? AND prompt_version_id=?", (run["id"], candidate))}
    assert prior_outputs
    budget = run["budget"]
    response = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget", json={
        "revision": run["revision"], "limits": {"total_limit": budget["total_limit"] + 1000,
        "search_limit": budget["search_limit"] + 1000, "acceptance_limit": budget["acceptance_limit"]}})
    assert response.status_code == 200, response.text
    client.post(f"/workflow-api/v1/runs/{run['id']}/resume")
    completed = wait_run(client, run["id"])
    assert completed["state"] == "completed", completed
    assert len(completed["candidates"]) == 1
    assert completed["candidates"][0]["prompt_version_id"] == candidate
    outputs = db.query("SELECT id,item_id FROM outputs WHERE run_id=? AND prompt_version_id=?", (run["id"], candidate))
    expected = len(run["snapshot"]["data"]["reflection_item_ids"])
    assert len(outputs) == len({row["item_id"] for row in outputs}) == expected
    assert prior_outputs <= {row["id"] for row in outputs}
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=? AND role='optimizer'", (run["id"],))["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM prompt_versions WHERE project_id=? AND origin='optimizer'", (data["pid"],))["n"] == 1

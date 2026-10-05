from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from prompt_lib.core import BizError
from prompt_lib.db import DB, get_db
from prompt_lib.engine import call_model
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.providers import CallResult
from .conftest import setup_project_with_data, start_run, wait_run


def call(db, ledger, budget, content="x"):
    return call_model(db, "generation", {"connection_id": "conn_mock"},
                      [{"role": "user", "content": content}], {}, "receipt", "fixed-operation",
                      "search", budget, ledger)


@pytest.mark.parametrize("usage", [{"in": 10, "out": 5}, {}])
def test_saved_response_replays_after_database_reopen_without_new_request(client, usage):
    db = get_db()
    limits = {"mode": "token", "total_limit": 10000, "search_limit": 10000, "acceptance_limit": 0}
    provider = SimpleNamespace(complete=Mock(return_value=CallResult("saved", "stop", usage)))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        first = call(db, Ledger(db), BudgetState(limits))
        reopened = DB(db.path)
        try:
            second = call(reopened, Ledger(reopened), BudgetState(limits))
        finally:
            reopened._conn.close()
        assert first.text == second.text == "saved"
        assert provider.complete.call_count == 1
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id='receipt'")["n"] == 1


def test_same_operation_with_changed_request_is_rejected(client):
    db = get_db()
    budget = BudgetState({"mode": "token", "total_limit": 10000, "search_limit": 10000, "acceptance_limit": 0})
    call(db, Ledger(db), budget)
    with pytest.raises(BizError) as error:
        call(db, Ledger(db), budget, "changed input")
    assert error.value.code == "CALL_IDENTITY_CONFLICT"


def test_partial_baseline_resumes_without_duplicate_generation(client):
    from prompt_lib.db import get_db
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:8], max_candidates=0,
                    budget={"mode": "token", "total_limit": 6000,
                            "search_limit": 5000, "acceptance_limit": 1000})
    assert run["state"] == "paused_budget", run
    db = get_db()
    before = db.query("SELECT id FROM outputs WHERE run_id=?", (run["id"],))
    assert before  # A real result exists before budget interruption.
    result = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget", json={
        "revision": run["revision"], "limits": {"total_limit": 100000, "search_limit": 90000, "acceptance_limit": 1000}})
    assert result.status_code == 200, result.text
    client.post(f"/workflow-api/v1/runs/{run['id']}/resume")
    completed = wait_run(client, run["id"])
    assert completed["state"] == "completed", completed
    expected = len(run["snapshot"]["data"]["reflection_item_ids"])
    rows = db.query("SELECT id,item_id FROM outputs WHERE run_id=?", (run["id"],))
    assert len(rows) == len({row["item_id"] for row in rows}) == expected
    assert {row["id"] for row in before} <= {row["id"] for row in rows}
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=? AND role='generation'", (run["id"],))["n"] == expected


def test_response_saved_before_output_insert_can_recover_write_interruption(client):
    from prompt_lib.domain import DataService, ProjectsService, PromptService
    from prompt_lib.engine import generate_once
    data = setup_project_with_data(client)
    db = get_db()
    project = ProjectsService(db).get(data["pid"])
    prompt = PromptService(db).get(data["prompt_id"])
    item = DataService(db).get_item_runtime(data["pid"], data["item_ids"][0])
    budget = BudgetState({"mode": "token", "total_limit": 10000, "search_limit": 10000, "acceptance_limit": 0})
    provider = SimpleNamespace(complete=Mock(return_value=CallResult("durable answer", "stop", {"in": 10, "out": 5})))
    original_execute = db.execute
    def interrupted_insert(sql, params=()):
        if sql.startswith("INSERT INTO outputs"):
            raise OSError("simulated interruption before output record")
        return original_execute(sql, params)
    args = (db, project, prompt, item, {"connection_id": "conn_mock"}, "gap", "search", budget, Ledger(db))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        with patch.object(db, "execute", side_effect=interrupted_insert), pytest.raises(OSError):
            generate_once(*args, logical_id="same-generation")
        assert db.one("SELECT COUNT(*) n FROM outputs WHERE run_id='gap'")["n"] == 0
        restored = generate_once(*args, logical_id="same-generation")
        assert restored["text"] == "durable answer"
        assert provider.complete.call_count == 1
    assert db.one("SELECT COUNT(*) n FROM outputs WHERE run_id='gap'")["n"] == 1

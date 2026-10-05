from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from .conftest import setup_project_with_data, start_run
from prompt_lib.db import get_db
from prompt_lib.domain import DataService, ProjectsService, PromptService
from prompt_lib.engine import generate_once
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.providers import CallResult, ProviderError


@pytest.mark.parametrize("retry_fails", [False, True])
@pytest.mark.parametrize("mode", ["token", "money"])
def test_confirmed_rejection_releases_only_original_and_allows_one_new_attempt(client, retry_fails, mode):
    data = setup_project_with_data(client)
    if mode == 'money':
        client.put('/workflow-api/v1/settings/prices', json={
            'mock-gen-1': {'in_per_1k': '0.001', 'out_per_1k': '0.002', 'currency': 'CNY'}})
    budget = {'mode': mode, 'total_limit': '1.5' if mode == 'money' else 1000000,
              'search_limit': '1.5' if mode == 'money' else 1000000, 'acceptance_limit': 0}
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0, budget=budget)
    db = get_db()
    failure = ProviderError("NETWORK", "uncertain response", retryable=False)
    provider = SimpleNamespace(complete=Mock(side_effect=[failure,
        failure if retry_fails else CallResult("new attempt response", "stop", {"in": 10, "out": 5})]))
    args = (db, ProjectsService(db).get(data["pid"]), PromptService(db).get(data["prompt_id"]),
            DataService(db).get_item_runtime(data["pid"], data["item_ids"][2]),
            run["snapshot"]["models"]["generation"], run["id"], "search", BudgetState(run["budget"]), Ledger(db))
    logical_id = "confirmed-rejection"
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        first = generate_once(*args, logical_id=logical_id)
        assert first["status"] == "failed"
        old = db.one("SELECT * FROM ledger WHERE run_id=? AND logical_id=?", (run["id"], f"{run['id']}:{logical_id}"))
        assert old["status"] == "sent_unknown" and old["reserved_tokens"] > 0
        body = {"request_hash": old["request_hash"], "reason": "供应商确认未受理", "evidence": "供应商请求查询明确未接收且无费用",
                "confirmed_not_accepted": True}
        url = f"/workflow-api/v1/runs/{run['id']}/ledger/{old['attempt_id']}/confirm-not-accepted"
        assert client.post(url, json={**body, "confirmed_not_accepted": "true"}).status_code == 422
        assert client.post(url, json={**body, "request_hash": "wrong"}).status_code == 409
        assert client.post(url, json={**body, "evidence": ""}).status_code == 422
        assert db.one("SELECT reserved_tokens FROM ledger WHERE attempt_id=?", (old["attempt_id"],))[0] == old["reserved_tokens"]
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json=body).status_code == 200
        released = db.one("SELECT * FROM ledger WHERE attempt_id=?", (old["attempt_id"],))
        assert released["status"] == "failed" and released["reserved_tokens"] == 0
        assert released["actual_cost"] == "0"
        assert released['reserved_amount'] == '0'
        assert provider.complete.call_count == 1  # Confirmation itself never dispatches.
        second = generate_once(*args, logical_id=logical_id)
        assert second["id"] != first["id"]
        assert second["status"] == ("failed" if retry_fails else "ok")
        assert generate_once(*args, logical_id=logical_id)["id"] == second["id"]
        assert provider.complete.call_count == 2
        attempts = db.query("SELECT * FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index", (run["id"], f"{run['id']}:{logical_id}"))
        assert len(attempts) == 2 and attempts[1]["attempt_id"] != old["attempt_id"]
        assert attempts[1]["status"] == ("sent_unknown" if retry_fails else "ok")
        if retry_fails:
            reserved = attempts[1]["reserved_tokens"]
            amount = attempts[1]['reserved_amount']
            assert reserved > 0
            assert client.post(url, json=body).status_code == 200
            assert db.one("SELECT reserved_tokens FROM ledger WHERE attempt_id=?", (attempts[1]["attempt_id"],))[0] == reserved
            assert db.one('SELECT reserved_amount FROM ledger WHERE attempt_id=?', (attempts[1]['attempt_id'],))[0] == amount
    assert db.one("SELECT COUNT(*) n FROM audit_log WHERE target=? AND action='ledger.confirm_not_accepted'", (old["attempt_id"],))[0] == 1

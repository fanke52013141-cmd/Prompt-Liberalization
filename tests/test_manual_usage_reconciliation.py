from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from .conftest import setup_project_with_data, start_run
from prompt_lib.db import get_db
from prompt_lib.engine import call_model
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.providers import CallResult, ProviderError


@pytest.mark.parametrize("finish,expected_status", [("stop", "ok"), ("length", "incomplete")])
@pytest.mark.parametrize("usage_known", [True, False])
@pytest.mark.parametrize("prior_attempts", [1, 2])
def test_recovered_response_supersedes_failed_output_without_another_dispatch(client, finish, expected_status, usage_known, prior_attempts):
    from prompt_lib.domain import DataService, ProjectsService, PromptService
    from prompt_lib.engine import generate_once
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0)
    db = get_db()
    errors = [ProviderError("NETWORK", "lost response", retryable=number < prior_attempts - 1)
              for number in range(prior_attempts)]
    provider = SimpleNamespace(complete=Mock(side_effect=errors))
    args = (db, ProjectsService(db).get(data["pid"]), PromptService(db).get(data["prompt_id"]),
            DataService(db).get_item_runtime(data["pid"], data["item_ids"][2]),
            run["snapshot"]["models"]["generation"], run["id"], "search", BudgetState(run["budget"]), Ledger(db))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        failed = generate_once(*args, logical_id="recovered-response")
        assert failed["status"] == "failed"
        attempt = db.one("SELECT * FROM ledger WHERE run_id=? AND status='sent_unknown' ORDER BY attempt_index", (run["id"],))
        url = f"/workflow-api/v1/runs/{run['id']}/ledger/{attempt['attempt_id']}/reconcile-response"
        body = {"request_hash": attempt["request_hash"], "reason": "恢复供应商原响应", "evidence": "供应商请求记录R123已核对",
                "text": "original provider output", "finish": finish, "usage_known": usage_known,
                "actual_in": 10 if usage_known else None, "actual_out": 5 if usage_known else None}
        assert client.post(url, json={**body, "finish": None}).status_code == 422
        if not usage_known:
            assert client.post(url, json={**body, "actual_in": 0}).status_code == 422
            assert client.post(url, json={**body, "usage_known": "false"}).status_code == 422
        assert db.one("SELECT status FROM ledger WHERE attempt_id=?", (attempt["attempt_id"],))[0] == "sent_unknown"
        assert client.post(url, json=body).status_code == 200
        if not usage_known:
            pending = db.one("SELECT * FROM ledger WHERE attempt_id=?", (attempt["attempt_id"],))
            assert pending["status"] == "usage_unknown"
            assert pending["actual_in"] is None and pending["actual_cost"] is None
            assert pending["reserved_tokens"] == attempt["reserved_tokens"]
            assert pending["reserved_amount"] == attempt["reserved_amount"]
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json={**body, "text": "different output"}).status_code == 409
        recovered = generate_once(*args, logical_id="recovered-response")
        assert recovered["status"] == expected_status
        assert recovered["text"] == body["text"]
        assert recovered["id"] != failed["id"]
        replay = generate_once(*args, logical_id="recovered-response")
        assert replay["id"] == recovered["id"]
        assert provider.complete.call_count == prior_attempts
        if not usage_known:
            usage_url = url.replace("reconcile-response", "reconcile-usage")
            usage_body = {"request_hash": attempt["request_hash"], "reason": "随后核实用量", "evidence": "供应商用量记录已收到",
                          "actual_in": 10, "actual_out": 5}
            assert client.post(usage_url, json=usage_body).status_code == 200
            assert client.post(url, json=body).status_code == 200
            settled = db.one("SELECT * FROM ledger WHERE attempt_id=?", (attempt["attempt_id"],))
            assert settled["status"] == "ok" and settled["reserved_tokens"] == 0
            assert settled["response_resolution_json"] and settled["resolution_json"]
    assert db.one("SELECT status FROM outputs WHERE id=?", (failed["id"],))[0] == "failed"
    action = "ledger.reconcile_response" if usage_known else "ledger.recover_response_usage_unknown"
    assert db.one("SELECT COUNT(*) n FROM audit_log WHERE action=? AND target=?", (action, attempt["attempt_id"]))[0] == 1


@pytest.mark.parametrize("mode", ["token", "money"])
def test_manual_usage_settlement_is_atomic_idempotent_and_replayable(client, mode):
    data = setup_project_with_data(client)
    if mode == "money":
        client.put("/workflow-api/v1/settings/prices", json={
            "mock-gen-1": {"in_per_1k": "0.1", "out_per_1k": "0.2", "currency": "CNY"}})
    budget = {"mode": mode, "total_limit": "100" if mode == "money" else 1000000,
              "search_limit": "100" if mode == "money" else 1000000, "acceptance_limit": 0}
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0, budget=budget)
    assert run["state"] == "completed", run
    db = get_db()
    provider = SimpleNamespace(complete=Mock(return_value=CallResult("saved result", "stop", {})))
    args = (db, "generation", run["snapshot"]["models"]["generation"],
            [{"role": "user", "content": "manual usage reconciliation"}], {}, run["id"],
            "manual-usage-test", "search", BudgetState(run["budget"]), Ledger(db))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        call_model(*args)
        attempts = client.get(f"/workflow-api/v1/runs/{run['id']}/ledger/attempts").json()["attempts"]
        row = next(row for row in attempts if row["status"] == "usage_unknown")
        assert not ({"api_key", "messages", "response_json"} & set(row))
        url = f"/workflow-api/v1/runs/{run['id']}/ledger/{row['attempt_id']}/reconcile-usage"
        body = {"request_hash": row["request_hash"], "reason": "核对缺失用量", "evidence": "供应商记录：输入13，输出7",
                "actual_in": 13, "actual_out": 7}
        invalid = client.post(url, json={**body, "request_hash": "wrong"})
        assert invalid.status_code == 409
        assert db.one("SELECT status FROM ledger WHERE attempt_id=?", (row["attempt_id"],))["status"] == "usage_unknown"
        assert client.post(url, json=body).status_code == 200
        before = db.one("SELECT budget_state_json FROM runs WHERE id=?", (run["id"],))[0]
        assert client.post(url, json=body).status_code == 200
        assert db.one("SELECT budget_state_json FROM runs WHERE id=?", (run["id"],))[0] == before
        assert client.post(url, json={**body, "actual_in": 14}).status_code == 409
        replay = call_model(*args)
        assert replay.text == "saved result" and replay.usage == {"in": 13, "out": 7}
        assert provider.complete.call_count == 1
    settled = db.one("SELECT * FROM ledger WHERE attempt_id=?", (row["attempt_id"],))
    assert settled["status"] == "ok" and settled["reserved_tokens"] == 0
    if mode == "money":
        assert settled["actual_cost"] == "0.0027"
    assert db.one("SELECT COUNT(*) n FROM audit_log WHERE target=? AND action='ledger.reconcile_usage'",
                  (row["attempt_id"],))["n"] == 1


def test_response_unknown_cannot_be_resolved_as_usage_only(client):
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0)
    db = get_db()
    provider = SimpleNamespace(complete=Mock(side_effect=ProviderError("NETWORK", "timeout", retryable=False)))
    with patch("prompt_lib.engine._get_provider", return_value=provider), pytest.raises(ProviderError):
        call_model(db, "generation", run["snapshot"]["models"]["generation"],
                   [{"role": "user", "content": "unknown response"}], {}, run["id"], "lost-response", "search",
                   BudgetState(run["budget"]), Ledger(db))
    row = db.one("SELECT * FROM ledger WHERE run_id=? AND status='sent_unknown'", (run["id"],))
    body = {"request_hash": row["request_hash"], "reason": "核对用量", "evidence": "未提供响应", "actual_in": 1, "actual_out": 2}
    url = f"/workflow-api/v1/runs/{run['id']}/ledger/{row['attempt_id']}/reconcile-usage"
    assert client.post(url, json={**body, "evidence": ""}).status_code == 422
    assert client.post(url, json={**body, "actual_in": True}).status_code == 422
    assert client.post(url, json=body).status_code == 409
    assert db.one("SELECT status,reserved_tokens FROM ledger WHERE attempt_id=?", (row["attempt_id"],))["reserved_tokens"] == row["reserved_tokens"]

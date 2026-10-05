from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from prompt_lib.core import BizError
from prompt_lib.db import get_db
from prompt_lib.engine import call_model
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.providers import CallResult, ProviderError


def invoke(db, budget, run_id, logical_id="a"):
    return call_model(db, "generation", {"connection_id": "conn_mock"},
                      [{"role": "user", "content": "x"}], {}, run_id, logical_id, "search",
                      budget, Ledger(db))


@pytest.mark.parametrize("usage", [{}, {"in": 3}, {"in": True, "out": 2},
                                    {"in": -1, "out": 2}, {"in": "3", "out": 2}, None])
def test_success_with_invalid_usage_keeps_response_and_budget(client, usage):
    db = get_db()
    budget = BudgetState({"mode": "token", "total_limit": 3000,
                          "search_limit": 3000, "acceptance_limit": 0})
    provider = SimpleNamespace(complete=Mock(return_value=CallResult("saved answer", "stop", usage)))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        result = invoke(db, budget, "unknown-test")
        assert result.text == "saved answer"
        row = db.one("SELECT * FROM ledger WHERE run_id='unknown-test'")
        assert row["status"] == "usage_unknown"
        assert row["reserved_tokens"] > 2048
        with pytest.raises(BizError) as error:
            invoke(db, budget, "unknown-test", "b")
        assert error.value.code == "BUDGET_EXHAUSTED"
        assert provider.complete.call_count == 1
    report = Ledger(db).reconcile("unknown-test")
    assert report["known_tokens"] == 0
    assert report["unknown_tokens"] == row["reserved_tokens"]
    assert report["usage_unknown_attempts"] == 1
    assert report["consistent"]


def test_uncertain_network_error_cannot_release_budget_for_retry(client):
    db = get_db()
    budget = BudgetState({"mode": "token", "total_limit": 3000,
                          "search_limit": 3000, "acceptance_limit": 0})
    provider = SimpleNamespace(complete=Mock(side_effect=ProviderError("NETWORK", "timeout", retryable=True)))
    with patch("prompt_lib.engine._get_provider", return_value=provider):
        with pytest.raises(BizError) as error:
            invoke(db, budget, "network-test")
        assert error.value.code == "BUDGET_EXHAUSTED"
    assert provider.complete.call_count == 1
    assert db.one("SELECT status FROM ledger WHERE run_id='network-test'")["status"] == "sent_unknown"


def test_known_usage_can_reconcile_previously_unknown_call_without_double_charge(client):
    db = get_db()
    ledger = Ledger(db)
    budget = BudgetState({"mode": "token", "total_limit": 3000,
                          "search_limit": 3000, "acceptance_limit": 0})
    aid = ledger.reserve("r", "a", "generation", "mock", "search", 1000, budget)
    ledger.mark_usage_unknown(aid, budget)
    ledger.mark_failed(aid, budget)
    assert db.one("SELECT status FROM ledger WHERE id=?", (aid,))["status"] == "usage_unknown"
    ledger.settle(aid, budget, 100, 200)
    ledger.settle(aid, budget, 100, 200)
    assert budget.spent["search"] == 300
    assert budget.reserved["search"] == 0

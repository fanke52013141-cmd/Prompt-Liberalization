import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from prompt_lib.core import BizError
from prompt_lib.db import DB
from prompt_lib.ledger import BudgetState, Ledger


def seed(db):
    limits = {"mode": "token", "total_limit": 100, "search_limit": 100, "acceptance_limit": 0}
    db.execute("INSERT INTO runs(id,project_id,snapshot_json,snapshot_hash,budget_state_json,created_at,updated_at) "
               "VALUES('r','p','{}','hash',?,'now','now')", (json.dumps(limits),))
    return limits


def test_separate_database_connections_and_stale_copies_cannot_overspend(tmp_path):
    first = DB(str(tmp_path / "budget.db"))
    limits = seed(first)
    second = DB(first.path)
    try:
        def reserve(index):
            db = first if index % 2 else second
            # Each caller deliberately believes nothing has been spent.
            stale = BudgetState({**limits, "search_limit": 1000, "total_limit": 1000})
            try:
                return Ledger(db).reserve("r", str(index), "generation", "mock", "search", 60, stale)
            except BizError as error:
                assert error.code == "BUDGET_EXHAUSTED"
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            attempts = list(pool.map(reserve, range(6)))
        assert sum(attempt is not None for attempt in attempts) == 1
        persisted = json.loads(first.one("SELECT budget_state_json FROM runs WHERE id='r'")[0])
        assert persisted["reserved"]["search"] == 60
        assert persisted["search_limit"] == 100
    finally:
        first._conn.close()
        second._conn.close()


def test_out_of_order_settlement_preserves_other_reservations_and_is_idempotent(tmp_path):
    db = DB(str(tmp_path / "budget.db"))
    try:
        limits = seed(db)
        ledger = Ledger(db)
        a, b = BudgetState(limits), BudgetState(limits)
        aid = ledger.reserve("r", "a", "generation", "mock", "search", 40, a)
        bid = ledger.reserve("r", "b", "evaluation", "mock", "search", 40, b)
        ledger.settle(bid, b, 10, 10)
        ledger.settle(aid, a, 5, 5)
        ledger.settle(bid, b, 10, 10)
        ledger.mark_failed(aid, a)  # A late error cannot erase a settled call.
        persisted = json.loads(db.one("SELECT budget_state_json FROM runs WHERE id='r'")[0])
        assert persisted["spent"]["search"] == 30
        assert persisted["reserved"]["search"] == 0
        with pytest.raises(BizError, match="重复结算"):
            ledger.settle(bid, b, 20, 20)
    finally:
        db._conn.close()

from decimal import Decimal
import json

import pytest

from prompt_lib.core import BizError
from prompt_lib.db import DB, get_db
from prompt_lib.ledger import BudgetState, Ledger
from .conftest import setup_project_with_data, wait_run
from .test_runs import _draft


def seed(db):
    config = {"mode": "money", "total_limit": "0.3", "search_limit": "0.3", "acceptance_limit": "0",
              "prices": {"m": {"in_per_1k": "0.1", "out_per_1k": "0.1", "currency": "CNY"}}}
    db.execute("INSERT INTO runs(id,project_id,snapshot_json,snapshot_hash,budget_state_json,created_at,updated_at) "
               "VALUES('r','p','{}','hash',?,'now','now')", (json.dumps(config),))
    return BudgetState(config)


def reserve(ledger, budget, logical):
    return ledger.reserve("r", logical, "generation", "m", "search", 2000, budget,
                          estimated_in=1000, estimated_out=1000)


def test_fractional_money_budget_uses_currency_and_exact_decimal_settlement(tmp_path):
    db = DB(str(tmp_path / "money.db"))
    try:
        budget, ledger = seed(db), Ledger(db)
        aid = reserve(ledger, budget, "a")
        assert budget.reserved["search"] == Decimal("0.2")
        with pytest.raises(BizError) as error:
            reserve(ledger, budget, "b")
        assert error.value.code == "BUDGET_EXHAUSTED"
        ledger.settle(aid, budget, 1000, 0)
        reserve(ledger, budget, "c")  # 0.1 spent + 0.2 reserved == 0.3 exactly.
        report = ledger.reconcile("r")
        assert report["money_by_currency"] == {"CNY": {"known": "0.1", "reserved": "0.2"}}
        phase = ledger.phase_summary("r", "search")
        assert phase["physical_attempts"] == 2
        assert phase["settled_attempts"] == 1
        assert phase["pending_attempts"] == 1
        assert phase["known_tokens"] == 1000
        assert phase["unknown_token_reservation"] == 2000
        assert phase["actual_cost_estimate_by_currency"] == {"CNY": "0.1"}
        assert phase["reserved_cost_upper_bound_by_currency"] == {"CNY": "0.2"}
        row = db.one("SELECT actual_cost,currency FROM ledger WHERE id=?", (aid,))
        assert row["actual_cost"] == "0.1"
        assert row["currency"] == "CNY"
    finally:
        db._conn.close()


def test_unknown_usage_retains_money_reservation(tmp_path):
    db = DB(str(tmp_path / "money.db"))
    try:
        budget, ledger = seed(db), Ledger(db)
        aid = reserve(ledger, budget, "a")
        ledger.mark_usage_unknown(aid, budget)
        with pytest.raises(BizError):
            reserve(ledger, budget, "b")
        assert ledger.reconcile("r")["money_by_currency"]["CNY"]["reserved"] == "0.2"
    finally:
        db._conn.close()


def test_run_resolves_default_models_and_freezes_prices(client):
    data = setup_project_with_data(client)
    price = {"in_per_1k": "0.001", "out_per_1k": "0.002", "currency": "CNY"}
    assert client.put("/workflow-api/v1/settings/prices", json={"mock-gen-1": price}).status_code == 200
    draft = _draft(data, optimization={"max_candidates": 0},
                   budget={"mode": "money", "total_limit": "1.5", "search_limit": "1.0", "acceptance_limit": "0.5"})
    result = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs", json=draft)
    assert result.status_code == 202, result.text
    run = wait_run(client, result.json()["id"])
    assert run["state"] == "completed", run
    assert run["snapshot"]["budget"]["prices"]["mock-gen-1"] == price
    assert client.put("/workflow-api/v1/settings/prices", json={"mock-gen-1": {**price, "in_per_1k": "999"}}).status_code == 200
    rows = get_db().query("SELECT actual_in,actual_out,actual_cost FROM ledger WHERE run_id=?", (run["id"],))
    expected = sum((Decimal("0.001") * row["actual_in"] + Decimal("0.002") * row["actual_out"]) / 1000 for row in rows)
    assert Decimal(run["budget"]["spent"]["search"]) == expected
    assert all(Decimal(row["actual_cost"]) == (Decimal("0.001") * row["actual_in"] + Decimal("0.002") * row["actual_out"]) / 1000 for row in rows)


@pytest.mark.parametrize("price", [
    {"in_per_1k": "NaN", "out_per_1k": 1, "currency": "CNY"},
    {"in_per_1k": -1, "out_per_1k": 1, "currency": "CNY"},
    {"in_per_1k": 1, "out_per_1k": True, "currency": "CNY"},
    {"in_per_1k": 1, "out_per_1k": 1, "currency": ""},
])
def test_invalid_price_cannot_enable_money_budget(client, price):
    data = setup_project_with_data(client)
    client.put("/workflow-api/v1/settings/prices", json={"mock-gen-1": price})
    draft = _draft(data, budget={"mode": "money", "total_limit": 1, "search_limit": 1, "acceptance_limit": 0})
    result = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs/validate", json=draft)
    assert result.status_code == 422
    assert result.json()["code"] == "PRICE_INVALID"


def test_mixed_currencies_cannot_share_budget(client):
    data = setup_project_with_data(client)
    price = {"in_per_1k": 1, "out_per_1k": 1, "currency": "CNY"}
    client.put("/workflow-api/v1/settings/prices", json={"mock-gen-1": price, "other": {**price, "currency": "USD"}})
    draft = _draft(data, budget={"mode": "money", "total_limit": 1, "search_limit": 1, "acceptance_limit": 0})
    draft["models"]["evaluation"]["model"] = "other"
    result = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs/validate", json=draft)
    assert result.status_code == 422
    assert result.json()["code"] == "PRICE_CURRENCY_CONFLICT"


def test_money_migration_backs_up_old_database_once(tmp_path):
    import sqlite3
    from prompt_core.backup import verify_backup
    path = str(tmp_path / "old.db")
    db = DB(path)
    db._conn.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version='main_002_money_reservations'")
        conn.execute("ALTER TABLE ledger DROP COLUMN reserved_amount")
        conn.execute("ALTER TABLE ledger DROP COLUMN price_json")
    upgraded = DB(path)
    upgraded._conn.close()
    manifests = list((tmp_path / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    snapshot, metadata = verify_backup(manifests[0])
    assert all(version != "main_002_money_reservations" for version, _ in metadata["migrations"])
    with sqlite3.connect(snapshot) as conn:
        assert "reserved_amount" not in {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
    restarted = DB(path)
    restarted._conn.close()
    assert len(list((tmp_path / "backups").glob("*/manifest.json"))) == 1

"""Golden values and adversarial evidence cases for the shared rules."""
import sqlite3

import pytest

from prompt_core.decision import adoption_decision
from prompt_core.migrations import add_column, apply_migrations, sql
from prompt_core.statistics import exact_mcnemar, missing_bounds, summarize_pairs


def test_shared_mcnemar_golden_and_empty():
    assert exact_mcnemar(20, 4) == pytest.approx(0.0015438795, abs=1e-10)
    assert exact_mcnemar(0, 0) == 1.0


def test_missing_denominators_and_asymmetric_unknown():
    assert missing_bounds(50, 20, 10)["low"] == 0.625
    pairs = [{"baseline": None, "candidate": True}] * 10
    s = summarize_pairs(pairs)
    assert s["total"] == 10 and s["known"]["fix"] == 0
    assert s["worst_case"]["gain"] == 0 and s["worst_case"]["p"] == 1
    assert s["best_case"]["gain"] == 1
    assert s["bounds"]["candidate"]["low"] == 1


def test_unknown_can_reverse_an_observed_improvement():
    s = summarize_pairs([{"baseline": False, "candidate": True}] * 6 +
                        [{"baseline": True, "candidate": None}] * 10)
    assert s["known"]["gain"] == 1
    assert s["worst_case"]["gain"] < 0


def test_every_required_gate_must_be_true():
    checks = dict.fromkeys(("binding", "evidence", "sample", "safety", "protocol"), True)
    assert adoption_decision(True, checks)["eligible"]
    for gate in checks:
        invalid = dict(checks, **{gate: None})
        assert not adoption_decision(True, invalid)["eligible"]
    assert not adoption_decision(False, checks)["eligible"]


def test_migration_is_idempotent_and_rollback_preserves_old_data():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE examples (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO examples VALUES(1,'keep')")
        conn.commit()
        changes = [("1", (add_column("examples", "new_value", "TEXT DEFAULT ''"),))]
        assert apply_migrations(conn, changes) == ["1"]
        assert apply_migrations(conn, changes) == []
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn, [("2", (sql("CREATE TABLE should_rollback (id INTEGER)"),
                                           sql("INVALID SQL")))])
        assert conn.execute("SELECT value FROM examples").fetchone()[0] == "keep"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone()
        assert not conn.execute("SELECT 1 FROM schema_migrations WHERE version='2'").fetchone()
        with pytest.raises(RuntimeError, match="checksum"):
            apply_migrations(conn, [("1", (sql("CREATE TABLE modified (id INTEGER)"),))])
    finally:
        conn.close()

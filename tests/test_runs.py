"""运行快照/幂等/预算/无提升路径（TC031/TC032/TC033/TC035/TC037）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import setup_project_with_data, start_run, wait_run


def _draft(s, **over):
    from prompt_lib.db import get_db
    man = get_db().one("SELECT id FROM split_manifests WHERE project_id=? LIMIT 1", (s["pid"],))
    d = {
        "mode": "explore", "prompt": {"baseline_id": s["prompt_id"]},
        "manifest_id": man["id"] if man else "",
        "rubric_id": s["rubric_id"], "judge_id": None,
        "data": {"dev_item_ids": s["item_ids"][:4]},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_candidates": 1, "dev_sample_size": 4, "min_delta": 0.02},
        "budget": {"mode": "token", "total_limit": 10_000_000, "search_limit": 8_000_000,
                   "acceptance_limit": 1_000_000},
    }
    d.update(over)
    return d


def test_money_budget_without_prices_rejected_token_mode_ok(client):
    """TC032：价格未知时金额硬预算被阻止，token限额模式可用。"""
    s = setup_project_with_data(client)
    draft = _draft(s, budget={"mode": "money", "total_limit": 100, "search_limit": 80,
                              "acceptance_limit": 20})
    r = client.post(f"/workflow-api/v1/projects/{s['pid']}/runs/validate", json=draft)
    assert r.status_code == 422
    assert r.json()["code"] == "PRICE_UNKNOWN"
    draft["budget"] = {"mode": "token", "total_limit": 100000, "search_limit": 80000,
                       "acceptance_limit": 10000}
    r = client.post(f"/workflow-api/v1/projects/{s['pid']}/runs/validate", json=draft)
    assert r.status_code == 200


def test_phase_budget_must_not_exceed_total(client):
    """TC033前半：阶段额度之和不能超过总额度。"""
    s = setup_project_with_data(client)
    draft = _draft(s, budget={"mode": "token", "total_limit": 1000, "search_limit": 900,
                              "acceptance_limit": 500})
    r = client.post(f"/workflow-api/v1/projects/{s['pid']}/runs/validate", json=draft)
    assert r.status_code == 422
    assert r.json()["code"] == "BUDGET_PHASE_CONFLICT"


def test_idempotency_same_key_same_payload_same_run(client):
    """TC035：同键同内容返回同一run；双击只建一次。"""
    s = setup_project_with_data(client)
    r1 = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                   dev_ids=s["item_ids"][:4], idem_key="K1")
    r2 = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                   dev_ids=s["item_ids"][:4], idem_key="K1")
    assert r1["id"] == r2["id"]


def test_idempotency_same_key_different_payload_conflict(client):
    s = setup_project_with_data(client)
    start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
              dev_ids=s["item_ids"][:4], idem_key="K2")
    r = client.post(f"/workflow-api/v1/projects/{s['pid']}/runs",
                    json=_draft(s), headers={"Idempotency-Key": "K2"})
    assert r.status_code == 409
    assert r.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_no_improvement_keeps_baseline_and_completes(client):
    """TC037：模拟无提升 -> 运行completed，保留基线，结论不为提升。"""
    s = setup_project_with_data(client)
    run = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:4], max_candidates=0)
    assert run["state"] == "completed"
    assert run["stop_reason"] == "no_improvement"
    assert run["locked_candidate"] == ""
    assert run["candidates"] == []


def test_budget_exhausted_pauses_run_and_never_touches_acceptance_reserve(client):
    """TC033后半+预算暂停：搜索额度耗尽 -> paused_budget；验收预留不动。"""
    s = setup_project_with_data(client)
    run = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:4], max_candidates=1,
                    budget={"mode": "token", "total_limit": 3000, "search_limit": 2000,
                            "acceptance_limit": 1000})
    assert run["state"] == "paused_budget"
    assert run["stop_reason"] == "budget_exhausted"
    assert run["budget"]["spent"].get("acceptance", 0) == 0
    assert run["budget"]["reserved"].get("acceptance", 0) == 0


def test_ledger_reconciles_attempts(client):
    """TC057：账本各attempt求和一致，sent_unknown单列。"""
    s = setup_project_with_data(client)
    run = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:3], max_candidates=1)
    assert run["state"] == "completed"
    led = client.get(f"/workflow-api/v1/runs/{run['id']}/ledger").json()
    assert led["attempts"] >= 2  # 基线生成+评价至少各一次
    assert led["consistent"] is True
    roles = set(led["by_role"].keys())
    assert {"generation", "evaluation"} <= roles

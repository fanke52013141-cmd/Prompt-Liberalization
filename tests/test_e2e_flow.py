"""端到端闭环：案例→标准→基线→优化→候选→独立验收→采用→反馈。

软件验收与提示词效果验收分开（验收方案第1节）：
无论最终决策是 verified_improvement 还是 no_improvement，
只要流程完整、状态可解释、账本一致，软件验收即通过。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import setup_project_with_data, start_run, wait_run


def _wait_state(client, getter, want, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if getter() == want:
            return True
        time.sleep(0.2)
    return False


def test_full_loop_case_to_feedback(client):
    s = setup_project_with_data(client)
    pid = s["pid"]

    # 1) 准备度随依赖推进（P02）
    rd = client.get(f"/workflow-api/v1/projects/{pid}/readiness").json()
    assert rd["state"] in ("rubric_ready", "judge_ready", "experiment_ready")

    # 2) 探索模式运行（允许无审计评价器：允许退回人工评价继续探索）
    run = start_run(client, pid, s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:6], max_candidates=2)
    assert run["state"] == "completed", run
    assert run["stop_reason"] in ("candidate_found", "no_improvement")
    if run["stop_reason"] == "candidate_found":
        best = max(run["candidates"], key=lambda c: c["score"])
        assert best["parent_id"] == s["prompt_id"]  # 候选可追溯父版本（TC041）

    # 3) 锁定候选（TC042 前置）
    r = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert r.status_code == 409  # 未锁定不允许验收
    target = "baseline" if run["stop_reason"] == "no_improvement" \
        else max(run["candidates"], key=lambda c: c["score"])["candidate_id"]
    lk = client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={"candidate_id": target})
    assert lk.status_code == 200
    lk2 = client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={"candidate_id": "baseline"})
    assert lk2.status_code == 409  # 只能锁一个挑战者

    # 4) 独立验收：解封封存集，同输入同模型同参数（TC042）
    acc = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert acc.status_code == 202, acc.text
    rep = acc.json()
    assert rep["decision"] in ("verified_improvement", "no_improvement", "regression",
                               "inconclusive", "evaluation_invalid")
    st = rep["stats"]
    assert st["group_n"] + st["unknown"] == st["sealed_total"]  # 未知项不删除（TC046）
    # 测试集已消耗：再次验收被拒（TC043）
    acc2 = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert acc2.status_code == 409
    assert acc2.json()["code"] == "TEST_ALREADY_CONSUMED"

    # 5) 采用：verified 才能active；否则trial
    if rep["decision"] == "verified_improvement":
        cand_pv = [c for c in run["candidates"] if c["candidate_id"] == target][0]["prompt_version_id"] \
            if target != "baseline" else s["prompt_id"]
        rel = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                          json={"prompt_version_id": cand_pv, "report_ref": rep["id"],
                                "mode": "active"})
        assert rel.status_code == 201, rel.text
        cur = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"]
        assert cur["prompt_version_id"] == cand_pv
        assert cur["report_ref"] == rep["id"]
        release_id = cur["id"]
    else:
        rel = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                          json={"prompt_version_id": s["prompt_id"], "mode": "trial"})
        assert rel.status_code == 201
        assert client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"] is None
        release_id = rel.json()["id"]

    # 6) 使用反馈（TC050）
    fb = client.post(f"/workflow-api/v1/releases/{release_id}/feedback",
                     json={"adoption": "minor_edit", "edit_time": "10分钟", "reason": "语气再柔和些"})
    assert fb.status_code == 201
    assert fb.json()["status"] == "pending_review"

    # 7) 账本可对账（TC057）
    led = client.get(f"/workflow-api/v1/runs/{run['id']}/ledger").json()
    assert led["consistent"] is True
    assert led["attempts"] >= 4

    # 8) 审计可追溯（FR25）
    audit = client.get("/workflow-api/v1/audit").json()["audit"]
    actions = {a["action"] for a in audit}
    assert {"import.commit", "manifest.freeze", "rubric.publish"} <= actions


def test_trial_then_verified_adopt_replaces_pointer(client):
    """试用不覆盖正式指针；verified报告可采用并更新指针（TC048）。"""
    s = setup_project_with_data(client)
    pid = s["pid"]
    run = start_run(client, pid, s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:6], max_candidates=2)
    assert run["state"] == "completed"
    target = "baseline" if run["stop_reason"] == "no_improvement" \
        else max(run["candidates"], key=lambda c: c["score"])["candidate_id"]
    client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={"candidate_id": target})
    rep = client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()
    if rep["decision"] == "verified_improvement":
        cand_pv = [c for c in run["candidates"] if c["candidate_id"] == target][0]["prompt_version_id"] \
            if target != "baseline" else s["prompt_id"]
        client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": s["prompt_id"], "mode": "trial"})
        cur = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"]
        assert cur is None  # trial不覆盖指针
        client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": cand_pv, "report_ref": rep["id"], "mode": "active"})
        cur = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"]
        assert cur["prompt_version_id"] == cand_pv

"""发布指针/试用边界/回滚/反馈/归档（TC003/TC048/TC049/TC050）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import make_project


def _insert_report(pid, prompt_id, decision):
    """直接构造验收报告行（完整验收流程由e2e覆盖）。"""
    from prompt_lib.core import new_id, now_iso
    from prompt_lib.db import get_db
    rid = new_id("rep")
    get_db().execute(
        "INSERT INTO acceptance_reports(id,run_id,project_id,candidate_ref,baseline_ref,"
        "test_manifest_json,policy_json,stats_json,decision,consumed,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?, '1', ?)",
        (rid, "run_seed", pid, prompt_id, "pv_base", "{}", "{}",
         json.dumps({"diff": 0.2}), decision, now_iso()))
    return rid


def test_active_requires_verified_report(client):
    """TC048：未验证候选只能trial；正式采用必须绑定verified报告。"""
    s = _setup(client)
    pid, pv1 = s["pid"], s["prompt_id"]
    # 无报告 -> 422
    r = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": pv1, "mode": "active"})
    assert r.status_code == 422
    # 非verified报告 -> 拒绝
    rid = _insert_report(pid, pv1, "no_improvement")
    r = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": pv1, "report_ref": rid, "mode": "active"})
    assert r.status_code == 422
    assert r.json()["code"] == "REPORT_NOT_VERIFIED"
    # trial 不需要报告，也不改变正式指针
    r = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": pv1, "mode": "trial"})
    assert r.status_code == 201
    assert client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"] is None


def test_concurrent_adopt_revision_conflict_and_rollback(client):
    """TC049：乐观锁防并发双采用；回滚恢复文本+模型+模板且不删历史。"""
    s = _setup(client)
    pid, pv1, pv2 = s["pid"], s["prompt_id"], s["prompt2_id"]
    rep = _insert_report(pid, pv1, "verified_improvement")
    r = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": pv1, "report_ref": rep, "mode": "active"})
    assert r.status_code == 201, r.text
    # 并发采用第二个版本：携带过期revision -> 409（先给它一份verified报告，排除报告校验干扰）
    rep2 = _insert_report(pid, pv2, "verified_improvement")
    r = client.post(f"/workflow-api/v1/projects/{pid}/releases",
                    json={"prompt_version_id": pv2, "report_ref": rep2, "mode": "active",
                          "expected_revision": 999})
    assert r.status_code == 409
    assert r.json()["code"] == "REVISION_CONFLICT"
    # 回滚到pv1的发布：新事件产生，历史保留
    hist = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()
    cur = hist["current"]
    rb = client.post(f"/workflow-api/v1/releases/{cur['id']}/rollback",
                     json={"target_release_id": hist["history"][-1]["id"]})
    assert rb.status_code == 200, rb.text
    after = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()
    assert after["current"]["prompt_version_id"] == pv1
    statuses = [h["status"] for h in after["history"]]
    assert "rolled_back" in statuses and statuses.count("active") == 1


def test_feedback_validation_and_pending_pool(client):
    """TC050：负面反馈与missing保留；回流需核验不自动成测试。"""
    s = _setup(client)
    pid = s["pid"]
    rep = _insert_report(pid, s["prompt_id"], "verified_improvement")
    client.post(f"/workflow-api/v1/projects/{pid}/releases",
                json={"prompt_version_id": s["prompt_id"], "report_ref": rep, "mode": "active"})
    cur = client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"]
    r = client.post(f"/workflow-api/v1/releases/{cur['id']}/feedback",
                    json={"adoption": "abandoned", "reason": "不如旧版"})
    assert r.status_code == 201
    assert r.json()["status"] == "pending_review"
    bad = client.post(f"/workflow-api/v1/releases/{cur['id']}/feedback", json={"adoption": "great"})
    assert bad.status_code == 422
    fl = client.get(f"/workflow-api/v1/projects/{pid}/feedback").json()["feedback"]
    assert fl[0]["adoption"] == "abandoned"


def test_archived_project_rejects_new_paid_run(client):
    """TC003：归档后历史可读，新收费运行被拒，取消归档恢复。"""
    from conftest import setup_project_with_data
    s = setup_project_with_data(client)
    pid = s["pid"]
    # 历史可读
    assert client.get(f"/workflow-api/v1/projects/{pid}").json()["status"] == "active"
    client.post(f"/workflow-api/v1/projects/{pid}/archive")
    assert client.get(f"/workflow-api/v1/projects/{pid}").json()["status"] == "archived"
    from prompt_lib.db import get_db
    man = get_db().one("SELECT id FROM split_manifests WHERE project_id=? LIMIT 1", (pid,))
    draft = {
        "mode": "explore", "prompt": {"baseline_id": s["prompt_id"]},
        "rubric_id": s["rubric_id"], "judge_id": None,
        "manifest_id": man["id"] if man else "",
        "data": {"dev_item_ids": s["item_ids"][:2]},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_candidates": 0, "dev_sample_size": 2, "min_delta": 0},
        "budget": {"mode": "token", "total_limit": 100000, "search_limit": 80000,
                   "acceptance_limit": 10000},
    }
    r = client.post(f"/workflow-api/v1/projects/{pid}/runs/validate", json=draft)
    assert r.status_code == 409
    assert r.json()["code"] == "PROJECT_ARCHIVED"
    client.post(f"/workflow-api/v1/projects/{pid}/unarchive")
    r = client.post(f"/workflow-api/v1/projects/{pid}/runs/validate", json=draft)
    assert r.status_code == 200


def _setup(client):
    from conftest import setup_project_with_data
    s = setup_project_with_data(client)
    pv2 = client.post(f"/workflow-api/v1/projects/{s['pid']}/prompts", json={
        "name": "基线", "body": "第二版正文", "variables": ["question", "student_answer", "grade_level"]})
    assert pv2.status_code == 201
    s["prompt2_id"] = pv2.json()["id"]
    return s

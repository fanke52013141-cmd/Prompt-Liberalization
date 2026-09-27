"""优化1.0 改造验收测试：自由任务、完整证据优化器、显式改写失败、长度控制、
回退检查、ABCD 评级、人工参与、停止原因、五步进度与问题级统计（§13/§15）。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import setup_project_with_data, wait_run  # noqa: E402

API = "/workflow-api/v1"


def _poll_run(client, rid, states, timeout=30):
    t0 = time.time()
    run = None
    while time.time() - t0 < timeout:
        run = client.get(f"{API}/runs/{rid}").json()
        if run["state"] in states:
            return run
        time.sleep(0.2)
    raise TimeoutError(f"run {rid} not in {states}: {run and run['state']}")


def _start_raw_run(client, pid, prompt_id, rubric_id, dev_ids, optimization=None):
    mans = client.get(f"{API}/projects/{pid}/manifests").json()["manifests"]
    draft = {
        "mode": "explore", "prompt": {"baseline_id": prompt_id},
        "rubric_id": rubric_id, "judge_id": None,
        "manifest_id": mans[0]["id"] if mans else "",
        "data": {"dev_item_ids": dev_ids, "select_item_ids": []},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": optimization or {"max_rounds": 2, "dev_sample_size": 4,
                                         "min_delta": 0.02},
        "budget": {"mode": "token", "total_limit": 10_000_000, "search_limit": 8_000_000,
                   "acceptance_limit": 1_000_000},
    }
    r = client.post(f"{API}/projects/{pid}/runs", json=draft)
    assert r.status_code == 202, r.text
    return _poll_run(client, r.json()["id"],
                     ("completed", "failed", "cancelled", "paused_budget", "waiting_human"))


# ---------------- 任务定义自由化（§3.2）----------------

def test_custom_task_project_create_and_import(client):
    """自定义任务契约：解除四种场景限制；运行时/评价字段重名被拒（BR01前提）。"""
    contract = {
        "label": "周报质量检查",
        "goal": "减少空洞评语，让周报点评给出可执行改进项",
        "runtime_fields": [{"name": "report", "label": "周报原文"},
                           {"name": "team", "label": "团队", "required": False}],
        "evaluation_fields": [{"name": "manager_note", "label": "主管批注"}],
    }
    r = client.post(f"{API}/projects", json={"name": "周报点评优化", "description": "",
                                             "task_type": "custom", "contract": contract,
                                             "goal": contract["goal"]})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["contract"]["label"] == "周报质量检查"
    assert p["contract"]["goal"].startswith("减少空洞评语")
    names = [f["name"] for f in p["contract"]["runtime_fields"]]
    assert names == ["report", "team"]

    # 自定义字段可正常导入案例
    items = [{"case_id": "w001", "origin": "real",
              "runtime_input": {"report": "本周完成了登录模块", "team": "A组"},
              "evaluation_only": {"manager_note": "缺量化结果"}}]
    content = "\n".join(json.dumps(x, ensure_ascii=False) for x in items)
    b = client.post(f"{API}/projects/{p['id']}/imports/preview",
                    json={"fmt": "jsonl", "content": content}).json()
    assert b["valid"] == 1, b
    client.post(f"{API}/projects/{p['id']}/imports/{b['id']}/commit", json={})
    items_r = client.get(f"{API}/projects/{p['id']}/items").json()
    assert items_r["total"] == 1

    # 评价字段与运行时字段重名：拒绝（评价专用信息不得进入生成请求）
    bad = dict(contract, evaluation_fields=[{"name": "report", "label": "重复"}])
    r2 = client.post(f"{API}/projects", json={"name": "坏契约", "task_type": "custom",
                                              "contract": bad})
    assert r2.status_code == 422
    assert r2.json()["code"] == "CONTRACT_INVALID"


def test_template_projects_still_work(client):
    """历史兼容：模板创建路径不变（旧任务/配置不受新流程影响）。"""
    s = setup_project_with_data(client)
    p = client.get(f"{API}/projects/{s['pid']}").json()
    assert p["task_type"] == "student_feedback"
    assert [f["name"] for f in p["contract"]["runtime_fields"]] == \
        ["question", "student_answer", "grade_level", "reference_material"]


# ---------------- 优化器完整证据输入（§8.2）----------------

def test_optimizer_receives_full_evidence_and_hides_evaluation_only(client):
    from prompt_lib.engine import build_optimizer_messages
    from prompt_lib.domain import ProjectsService
    s = setup_project_with_data(client)
    project = ProjectsService().get(s["pid"])
    pv = {"id": s["prompt_id"], "body": "你是一名教研老师。请点评学员答案。",
          "variables": ["question"], "frozen_segments": [{"name": "安全声明", "text": "不得虚构学员错误。"}]}
    evidence = {"failures": [{"case_id": "c001", "item_id": s["item_ids"][0],
                              "runtime_input": {"question": "解方程2x+1=10"},
                              "output_text": "学员做法完全不对，重新做一遍。",
                              "dims": {"判断正确": 0},
                              "feedback": [{"problem": "错误归因：计算错误说成概念不清",
                                            "quote": "学员只是计算错了，AI却说概念不清",
                                            "expected": "区分列式思路与计算错误",
                                            "severity": "severe", "status": "confirmed_error",
                                            "tags": ["错误归因"], "remark": "备注原文不可覆盖"}]}],
                "correct": []}
    rubric = {"dimensions": [{"name": "判断正确", "anchors": {"0": "判断错误"}}],
              "severity_examples": ["虚构学员错误"]}
    history = [{"round": 1, "decision": "kept", "hypothesis": "H", "result": "scored",
                "rationale": "R"}]
    msgs = build_optimizer_messages(pv, project, rubric, evidence, history, 4000)
    flat = "\n".join(m["content"] for m in msgs)
    # 完整证据都在：输入、输出、专家问题/原话/期望/标签/备注、冻结组件、变量白名单、历史
    for token in ("解方程2x+1=10", "学员做法完全不对", "错误归因：计算错误说成概念不清",
                  "学员只是计算错了", "区分列式思路与计算错误", "错误归因", "备注原文不可覆盖",
                  "安全声明", "question", "<current_prompt>", "假设=H"):
        assert token in flat, f"优化器输入缺少证据：{token}"
    # evaluation_only 数据（参考答案）绝不进入优化器消息（BR01）
    assert "参考答案" not in flat.replace("severity_examples", "")


# ---------------- 改写失败显式报告 + 长度控制（§8.6）----------------

def test_rewrite_failure_reported_no_fallback_fragment(client):
    """优化器返回坏JSON：明确报告 rewrite_failed，不静默追加固定业务文本充当候选。"""
    from prompt_lib.db import get_db
    s = setup_project_with_data(client)
    # 先注入再启动：优化模型将返回坏JSON
    row = get_db().one("SELECT value_json FROM settings WHERE key='connections'")
    conns = json.loads(row["value_json"])
    for c in conns:
        if c["id"] == "conn_mock":
            c["mock_inject"] = "optimizer_bad_json"
    get_db().execute("UPDATE settings SET value_json=? WHERE key='connections'",
                     (json.dumps(conns, ensure_ascii=False),))
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                         s["item_ids"][:4],
                         optimization={"max_rounds": 1, "dev_sample_size": 4, "min_delta": 0.02})
    assert run["state"] == "completed", run
    assert run["candidates"] == []  # 没有伪造候选
    evs = client.get(f"{API}/runs/{run['id']}/events").json()["events"]
    assert any(e["type"] == "rewrite_failed" for e in evs)
    rounds = client.get(f"{API}/runs/{run['id']}").json()["rounds"]
    assert rounds and rounds[0]["status"] == "rewrite_failed"
    assert "改写失败" in rounds[0]["rationale"]


def test_length_limit_rejects_rewrite(client):
    """长度上限：超过上限的改写被拒绝并报告，不产生候选（控制提示词膨胀）。"""
    s = setup_project_with_data(client)
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                         s["item_ids"][:4],
                         optimization={"max_rounds": 1, "dev_sample_size": 4,
                                       "min_delta": 0.02, "length_limit_chars": 50})
    assert run["state"] == "completed", run
    assert run["candidates"] == []
    rounds = client.get(f"{API}/runs/{run['id']}").json()["rounds"]
    assert rounds[0]["status"] == "rewrite_failed"
    assert "长度上限" in rounds[0]["rationale"]


# ---------------- 回退检查与保留依据（§8.6）----------------

def test_decide_keep_blocks_regression_and_reports_rationale():
    from prompt_lib.engine import decide_keep
    prev = {"score": 1.0, "severe": 0, "length": 20}
    # 平均分提升但原有正确案例回退：必须否决
    d = decide_keep(prev, {"score": 1.5, "severe": 0, "length": 60, "regressions": 2,
                           "fixed_problems": 1, "open_problems_before": 3}, 0.02)
    assert d["decision"] == "discarded" and "回退" in d["rationale"]
    # 严重错误增加：不能由高分抵消
    d2 = decide_keep(prev, {"score": 1.5, "severe": 2, "length": 60, "regressions": 0,
                            "fixed_problems": 1, "open_problems_before": 3}, 0.02)
    assert d2["decision"] == "discarded" and "严重" in d2["rationale"]
    # 提升超过阈值且无回退：保留，依据包含具体数字
    d3 = decide_keep(prev, {"score": 1.5, "severe": 0, "length": 60, "regressions": 0,
                            "fixed_problems": 2, "open_problems_before": 3}, 0.02)
    assert d3["decision"] == "kept" and "1.000" in d3["rationale"] and "1.500" in d3["rationale"]
    # 提升不足：淘汰并说明阈值
    d4 = decide_keep(prev, {"score": 1.01, "severe": 0, "length": 60, "regressions": 0,
                            "fixed_problems": 0, "open_problems_before": 3}, 0.02)
    assert d4["decision"] == "discarded" and "未超过阈值" in d4["rationale"]


def test_run_rounds_record_decision_rationale(client):
    """正常路径：轮次记录假设、决策与依据；冻结段在候选中未被改写；候选可追溯父版本。"""
    s = setup_project_with_data(client)
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                         s["item_ids"][:4],
                         optimization={"max_rounds": 1, "dev_sample_size": 4, "min_delta": 0.02})
    assert run["state"] == "completed", run
    assert run["stop_reason"] == "candidate_found"
    assert len(run["rounds"]) == 1
    rd = run["rounds"][0]
    assert rd["status"] == "scored" and rd["decision"] == "kept"
    assert rd["hypothesis"]
    assert rd["length_chars"] > 0
    cand = run["candidates"][0]
    assert cand["parent_id"] == s["prompt_id"]
    pv = client.get(f"{API}/prompts/{cand['prompt_version_id']}").json()
    assert pv["frozen_segments"] == [{"name": "安全声明", "text": "不得虚构学员错误。"}]
    # 基线问题清单已随原始测评落库
    detail = client.get(f"{API}/runs/{run['id']}?detail=true").json()["baseline_detail"]
    assert "items" in detail and "problems" in detail


def test_stall_stop_reason_after_no_change_rounds(client):
    """连续无值得保留的改善：明确停止原因 stalled_no_gain，不是伪装成功。"""
    s = setup_project_with_data(client)
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                         s["item_ids"][:4],
                         optimization={"max_rounds": 3, "dev_sample_size": 4,
                                       "min_delta": 0.02, "stall_rounds": 2})
    assert run["state"] == "completed", run
    assert run["stop_reason"] == "stalled_no_gain"
    assert run["stall_count"] >= 2
    # 第1轮保留候选，第2、3轮无修改
    statuses = [r["status"] for r in run["rounds"]]
    assert statuses == ["scored", "no_change", "no_change"]


def test_human_in_loop_pauses_and_continues(client):
    """人工参与模式：每轮完成后 waiting_human，显式继续后推进直至完成（§8.5）。"""
    s = setup_project_with_data(client)
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                         s["item_ids"][:4],
                         optimization={"max_rounds": 2, "dev_sample_size": 4,
                                       "min_delta": 0.02, "human_in_loop": True})
    assert run["state"] == "waiting_human", run
    run2 = run
    for _ in range(4):
        r = client.post(f"{API}/runs/{run2['id']}/continue")
        assert r.status_code == 200, r.text
        run2 = _poll_run(client, run2["id"], ("completed", "waiting_human", "failed"))
        if run2["state"] == "completed":
            break
    assert run2["state"] == "completed", run2
    assert run2["round_no"] == 2  # 恢复后从第2轮继续，不重复第1轮


# ---------------- 专家意见 / 标签 / ABCD（§6.3—§6.5）----------------

def test_expert_feedback_tags_and_import_errors(client):
    s = setup_project_with_data(client)
    pid = s["pid"]
    item_id = s["item_ids"][0]
    fb = client.post(f"{API}/projects/{pid}/expert_feedback", json={
        "item_id": item_id, "problem": "错误归因：计算错误说成概念不清",
        "quote": "学员只是计算错了", "expected": "区分列式与计算",
        "severity": "severe", "status": "confirmed_error", "tags": ["错误归因"],
        "remark": "请保留我的原话"})
    assert fb.status_code == 201, fb.text
    fid = fb.json()["id"]
    # 备注原文不被系统覆盖
    got = client.get(f"{API}/projects/{pid}/expert_feedback").json()["feedback"][0]
    assert got["remark"] == "请保留我的原话"
    # 状态更新
    client.put(f"{API}/expert_feedback/{fid}", json={"status": "preference"})
    assert client.get(f"{API}/projects/{pid}/expert_feedback").json()["feedback"][0]["status"] \
        == "preference"
    # 标签合并：历史意见保留并追加目标标签
    t1 = client.post(f"{API}/projects/{pid}/tags", json={"name": "错误归因"}).json()
    t2 = client.post(f"{API}/projects/{pid}/tags", json={"name": "归因偏差"}).json()
    client.put(f"{API}/expert_feedback/{fid}", json={"tags": ["错误归因"]})
    m = client.post(f"{API}/tags/{t1['id']}/merge", json={"into_id": t2["id"]})
    assert m.status_code == 200
    merged = client.get(f"{API}/projects/{pid}/expert_feedback").json()["feedback"][0]["tags"]
    assert set(merged) == {"错误归因", "归因偏差"}
    # 批量导入：逐行报错，不静默丢行
    content = "\n".join([
        json.dumps({"case_id": "c001", "problem": "扣分过重", "severity": "severe"},
                   ensure_ascii=False),
        json.dumps({"case_id": "nope", "problem": "案例不存在"}, ensure_ascii=False),
        "{坏JSON",
        json.dumps({"case_id": "c001"}, ensure_ascii=False)])
    imp = client.post(f"{API}/projects/{pid}/expert_feedback/import",
                      json={"content": content}).json()
    assert imp["created"] == 1
    reasons = " ".join(e["reason"] for e in imp["errors"])
    assert "案例不存在" in reasons and "JSON解析失败" in reasons and "problem" in reasons


def test_abcd_rating_rules_and_case_reviews(client):
    s = setup_project_with_data(client)
    pid, item_id = s["pid"], s["item_ids"][0]
    # 默认 ABCD 草案（可编辑建议）
    rule = client.post(f"{API}/projects/{pid}/rating_rules").json()
    codes = [l["code"] for l in rule["levels"]]
    assert codes == ["A", "B", "C", "D"]
    # 修改产生新版本，历史不被重新解释
    v2 = client.put(f"{API}/rating_rules/{rule['id']}", json={
        "levels": rule["levels"] + [{"code": "E", "name": "测试", "trend": "flat",
                                     "meaning": "", "display_basis": ""}]}).json()
    assert v2["version_no"] == rule["version_no"] + 1
    # 发布 v2，提交评级
    client.post(f"{API}/rating_rules/{v2['id']}/publish")
    rv = client.post(f"{API}/projects/{pid}/case_reviews", json={
        "item_id": item_id, "rule_id": v2["id"], "rating": "B",
        "resolutions": [{"feedback_id": "fbk_x", "status": "fixed"}],
        "new_problems": [{"description": "语气生硬", "severity": "normal"}],
        "source": "human"})
    assert rv.status_code == 201, rv.text
    assert rv.json()["rating"] == "B"
    # 评级码必须属于规则
    rv_bad = client.post(f"{API}/projects/{pid}/case_reviews", json={
        "item_id": item_id, "rule_id": v2["id"], "rating": "Z"})
    assert rv_bad.status_code == 422


def test_abcd_auto_suggestion_rules(client):
    from prompt_lib.domain import FeedbackService, RatingService
    s = setup_project_with_data(client)
    pid, item_id = s["pid"], s["item_ids"][0]
    fsvc, rsvc = FeedbackService(), RatingService()
    rule = rsvc.create_default(pid)
    f1 = fsvc.add(pid, item_id, "错误归因", severity="severe", status="confirmed_error")
    f2 = fsvc.add(pid, item_id, "扣分过重", severity="normal", status="confirmed_error")
    # 全部解决 → A
    assert rsvc.suggest_review(pid, item_id, rule["id"],
                               {f1["id"]: "fixed", f2["id"]: "fixed"})["rating"] == "A"
    # 部分解决 → B
    assert rsvc.suggest_review(pid, item_id, rule["id"],
                               {f1["id"]: "fixed", f2["id"]: "open"})["rating"] == "B"
    # 全部未解决 → C
    assert rsvc.suggest_review(pid, item_id, rule["id"],
                               {f1["id"]: "open", f2["id"]: "open"})["rating"] == "C"
    # 严重新增问题 → 优先 D
    assert rsvc.suggest_review(pid, item_id, rule["id"],
                               {f1["id"]: "fixed", f2["id"]: "fixed"},
                               new_problems=[{"description": "虚构学员错误",
                                              "severity": "severe"}])["rating"] == "D"
    # 无法判断 → 单独记录
    assert rsvc.suggest_review(pid, item_id, rule["id"],
                               {f1["id"]: "unknown", f2["id"]: "fixed"})["rating"] == "cannot_judge"


# ---------------- 五步进度与问题级统计（§4.2/§9.3）----------------

def test_progress_endpoint_five_steps(client):
    s = setup_project_with_data(client)
    pid = s["pid"]
    pr = client.get(f"{API}/projects/{pid}/progress").json()
    keys = [st["key"] for st in pr["steps"]]
    assert keys == ["prepare", "confirm_eval", "baseline", "optimize", "verify"]
    assert pr["current"] == "baseline"  # 材料+标准已就绪，下一步原始测评
    run = _start_raw_run(client, pid, s["prompt_id"], s["rubric_id"], s["item_ids"][:4],
                         optimization={"max_rounds": 1, "dev_sample_size": 4, "min_delta": 0.02})
    assert run["state"] == "completed"
    pr2 = client.get(f"{API}/projects/{pid}/progress").json()
    by_key = {st["key"]: st["done"] for st in pr2["steps"]}
    assert by_key["prepare"] and by_key["confirm_eval"] and by_key["baseline"]
    assert by_key["optimize"]  # 第1轮保留了候选
    assert not by_key["verify"]


def test_acceptance_reports_problem_stats_and_evidence_scope(client):
    s = setup_project_with_data(client)
    pid = s["pid"]
    # 给封存案例登记专家问题：验收时应输出问题项统计
    sealed_id = s["item_ids"][10]
    client.post(f"{API}/projects/{pid}/expert_feedback", json={
        "item_id": sealed_id, "problem": "错误归因", "severity": "severe",
        "status": "confirmed_error"})
    run = _start_raw_run(client, pid, s["prompt_id"], s["rubric_id"], s["item_ids"][:4],
                         optimization={"max_rounds": 1, "dev_sample_size": 4, "min_delta": 0.02})
    assert run["state"] == "completed"
    target = max(run["candidates"], key=lambda c: c["score"])["candidate_id"]
    client.post(f"{API}/runs/{run['id']}/lock", json={"candidate_id": target})
    rep = client.post(f"{API}/runs/{run['id']}/accept").json()
    st = rep["stats"]
    ps = st["problem_stats"]
    assert ps["total_registered"] >= 1
    assert set(ps["resolution"].keys()) >= {"resolved", "partial", "unresolved", "unknown"}
    assert set(ps["abcd"].keys()) == {"A", "B", "C", "D", "cannot_judge", "not_rated"}
    assert sum(ps["abcd"].values()) == st["sealed_total"]  # 每个案例都有归属，不遗漏
    es = st["evidence_scope"]
    assert es["independent"]["set"] == "sealed_test" and es["independent"]["consumed"] is True
    assert "不构成独立证明" in es["optimization_sample"]["role"]


def test_acceptance_without_registered_problems_notes_scope(client):
    """封存案例无登记问题：明确说明按通用质量标准评价，不强行套用原问题解决率。"""
    s = setup_project_with_data(client)
    run = _start_raw_run(client, s["pid"], s["prompt_id"], s["rubric_id"], s["item_ids"][:4],
                         optimization={"max_rounds": 0, "dev_sample_size": 4})
    assert run["state"] == "completed"
    client.post(f"{API}/runs/{run['id']}/lock", json={"candidate_id": "baseline"})
    rep = client.post(f"{API}/runs/{run['id']}/accept").json()
    ps = rep["stats"]["problem_stats"]
    assert ps["total_registered"] == 0
    assert "不强行套用" in ps["note"]


# ---------------- 项目硬删除（测试阶段能力）与字段名约束 ----------------

def test_project_hard_delete_cascades(client):
    """删除项目：从属数据全部级联清理，项目本身404。"""
    s = setup_project_with_data(client)
    pid = s["pid"]
    r = client.delete(f"{API}/projects/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == pid
    assert client.get(f"{API}/projects/{pid}").status_code == 404
    from prompt_lib.db import get_db
    db = get_db()
    for t in ("dataset_items", "rubrics", "prompt_versions", "split_manifests",
              "sealed_artifacts", "runs", "tags", "expert_feedback", "rating_rules"):
        n = db.one(f"SELECT COUNT(*) AS c FROM {t} WHERE project_id=?", (pid,))["c"]
        assert n == 0, f"{t} 未级联删除"
    assert db.one("SELECT COUNT(*) AS c FROM projects WHERE id=?", (pid,))["c"] == 0
    # 再删一次：404
    r2 = client.delete(f"{API}/projects/{pid}")
    assert r2.status_code == 404


def test_field_name_forbidden_characters_rejected(client):
    """字段名会用作 {{字段名}} 占位：花括号/逗号/冒号等字符被拒。"""
    for badname in ("题{}目", "a,b", "x:y"):
        r = client.post(f"{API}/projects", json={
            "name": "坏字段名", "task_type": "custom",
            "contract": {"runtime_fields": [{"name": badname, "label": "题目"}]}})
        assert r.status_code == 422, badname
        assert r.json()["code"] == "CONTRACT_INVALID"


# ---------------- 内置示例项目（可用性引导：先看一遍完整流程） ----------------

def test_demo_seed_idempotent_and_complete(client):
    """示例项目：幂等生成；材料/专家意见/运行/报告齐全，五步全部完成。"""
    d1 = client.post(f"{API}/demo/seed").json()
    assert d1["created"] is True, d1
    d2 = client.post(f"{API}/demo/seed").json()
    assert d2["created"] is False and d2["project_id"] == d1["project_id"]
    pid = d1["project_id"]
    p = client.get(f"{API}/projects/{pid}").json()
    assert p["name"].startswith("示例：")
    fb = client.get(f"{API}/projects/{pid}/expert_feedback").json()["feedback"]
    assert len(fb) >= 3  # 专家意见已整理
    runs = client.get(f"{API}/projects/{pid}/runs").json()["runs"]
    assert runs and runs[0]["state"] == "completed"
    assert runs[0]["locked_candidate"]  # 已锁定待验证版本
    reps = client.get(f"{API}/projects/{pid}/reports").json()["reports"]
    assert len(reps) >= 1  # 独立验证报告已生成
    rel = client.get(f"{API}/projects/{pid}/releases").json()
    assert rel["current"] or rel["history"]
    pr = client.get(f"{API}/projects/{pid}/progress").json()
    assert all(s["done"] for s in pr["steps"]), pr


# ---------------- 自定义任务的评价标准草案（演练中发现的阻断性 bug 回归） ----------------

def test_custom_project_rubric_draft_generic_and_contract_dims(client):
    """自定义任务：无维度时给通用草案；契约自带维度时直接采用；均可发布。"""
    base = {"runtime_fields": [{"name": "brief", "label": "Brief"}],
            "evaluation_fields": [{"name": "director_note", "label": "批注"}]}
    p1 = client.post(f"{API}/projects", json={"name": "通用草案", "task_type": "custom",
                                              "contract": base}).json()
    r1 = client.post(f"{API}/projects/{p1['id']}/rubrics")
    assert r1.status_code == 201, r1.text
    assert len(r1.json()["schema"]["dimensions"]) >= 1
    assert client.post(f"{API}/rubrics/{r1.json()['id']}/publish").status_code == 200

    dims = [{"name": "洞察深入", "anchors": {"0": "无洞察", "1": "表面",
             "2": "有洞察", "3": "洞察精准且新"}},
            {"name": "分镜可执行", "anchors": {"0": "无法执行", "1": "含糊",
             "2": "基本可执行", "3": "逐秒可拍"}}]
    p2 = client.post(f"{API}/projects", json={"name": "自带维度", "task_type": "custom",
                                              "contract": dict(base, dimensions=dims)}).json()
    r2 = client.post(f"{API}/projects/{p2['id']}/rubrics").json()
    assert [d["name"] for d in r2["schema"]["dimensions"]] == ["洞察深入", "分镜可执行"]
    assert client.post(f"{API}/rubrics/{r2['id']}/publish").status_code == 200

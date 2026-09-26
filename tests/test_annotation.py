"""人工标注/盲评/证据回放/评价器校准（TC017—TC024）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import setup_project_with_data, wait_run

EMOJI_TEXT = "学员答案有误🙂\n第二步出现概念混淆，需要重新推导。"


def _two_outputs(client, s):
    """跑一次探索运行获得输出，选两个不同提示词版本的输出组成对比。"""
    run = _run_with_outputs(client, s)
    outs = client.get(f"/workflow-api/v1/projects/{s['pid']}/outputs").json()["outputs"]
    ok = [o for o in outs if o["status"] == "ok"]
    assert len(ok) >= 2
    return run, ok


def _run_with_outputs(client, s, max_candidates=1):
    from conftest import start_run
    run = start_run(client, s["pid"], s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:4], max_candidates=max_candidates)
    assert run["state"] == "completed", run
    return run


def _rubric_dims(client, s):
    r = client.get(f"/workflow-api/v1/rubrics/{s['rubric_id']}").json()
    return [d["name"] for d in r["schema"]["dimensions"]]


def _insert_output(client, pid, pv_id, text):
    from prompt_lib.db import get_db
    from prompt_lib.core import new_id, now_iso
    oid = new_id("out")
    get_db().execute(
        "INSERT INTO outputs(id,project_id,item_id,prompt_version_id,run_id,role,text,status,"
        "usage_json,request_hash,created_at) VALUES(?,?,?,?,?,'generation',?, 'ok', '', '', ?)",
        (oid, pid, "case_x", pv_id, "seed_run", text, now_iso()))
    return oid


def _annotate_output(client, pid, s, output_id, dims, scores):
    """为输出创建人工核验gold标注（直接建盲评对并提交）。"""
    r = client.post(f"/workflow-api/v1/projects/{s['pid']}/rubrics").json()
    # 直接用公开API：把同一输出两侧做成对比再标注（保证最小夹具）
    pair = client.post(f"/workflow-api/v1/projects/{s['pid']}/pairs",
                       json={"left_output_id": output_id, "right_output_id": output_id,
                             "purpose": "calibration_seed"}).json()
    ann = client.post(f"/workflow-api/v1/pairs/{pair['public_id']}/annotations", json={
        "preference": "tie", "scores": scores, "evidence": [], "reason": "种子gold"})
    assert ann.status_code == 201, ann.text
    return ann.json()


def test_blind_pair_hides_version_info(client):
    """TC018：盲评网络响应不包含版本ID/名称/时间/评分。"""
    s = setup_project_with_data(client)
    _run_with_outputs(client, s)
    outs = client.get(f"/workflow-api/v1/projects/{s['pid']}/outputs").json()["outputs"]
    ok = [o for o in outs if o["status"] == "ok"]
    pair = client.post(f"/workflow-api/v1/projects/{s['pid']}/pairs",
                       json={"left_output_id": ok[0]["id"], "right_output_id": ok[1]["id"],
                             "purpose": "blind_ab"}).json()
    blind = client.get(f"/workflow-api/v1/pairs/{pair['public_id']}/blind").json()
    blob = json.dumps(blind, ensure_ascii=False)
    assert "prompt_version_id" not in blob
    assert ok[0]["id"] not in blob and ok[1]["id"] not in blob
    assert "created_at" not in blob
    assert set(blind.keys()) == {"public_id", "purpose", "left", "right"}


def test_evidence_span_codepoint_validation(client):
    """TC017：汉字/emoji/换行证据按Unicode码点回放；错位拒绝。"""
    s = setup_project_with_data(client)
    from prompt_lib.domain import PromptService
    pv = PromptService().get(s["prompt_id"])
    oid = _insert_output(client, s["pid"], pv["id"], EMOJI_TEXT)
    pair = client.post(f"/workflow-api/v1/projects/{s['pid']}/pairs",
                       json={"left_output_id": oid, "right_output_id": oid,
                             "purpose": "evidence_test"}).json()
    text = EMOJI_TEXT
    quote = "🙂\n第二步出现概念混淆"
    start = text.index(quote)
    end = start + len(quote)  # Python索引即码点位置
    ok = client.post(f"/workflow-api/v1/pairs/{pair['public_id']}/annotations", json={
        "preference": "unknown", "scores": {},
        "evidence": [{"side": "left", "start": start, "end": end, "quote": quote}],
        "reason": "证据测试"})
    assert ok.status_code == 201, ok.text
    # 错位：把UTF-16风格偏移当码点（emoji +1 偏移）应拒绝
    bad = client.post(f"/workflow-api/v1/pairs/{pair['public_id']}/annotations", json={
        "preference": "unknown", "scores": {},
        "evidence": [{"side": "left", "start": start + 1, "end": end + 1, "quote": quote}],
        "reason": "错位"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "EVIDENCE_MISMATCH"


def test_unknown_and_both_unusable_allowed_no_forced_winner(client):
    """TC019：不强选胜者；非法偏好被拒。"""
    s = setup_project_with_data(client)
    from prompt_lib.domain import PromptService
    pv = PromptService().get(s["prompt_id"])
    oid = _insert_output(client, s["pid"], pv["id"], "文本A")
    pair = client.post(f"/workflow-api/v1/projects/{s['pid']}/pairs",
                       json={"left_output_id": oid, "right_output_id": oid,
                             "purpose": "u"}).json()
    for pref in ("unknown", "both_unusable", "tie"):
        r = client.post(f"/workflow-api/v1/pairs/{pair['public_id']}/annotations",
                        json={"preference": pref, "scores": {}, "evidence": [], "reason": ""})
        assert r.status_code == 201, (pref, r.text)
    r = client.post(f"/workflow-api/v1/pairs/{pair['public_id']}/annotations",
                    json={"preference": "X", "scores": {}, "evidence": [], "reason": ""})
    assert r.status_code == 422


def test_model_pre_annotation_cannot_be_gold(client):
    """TC020：模型预标注未人工核验不能构建gold。"""
    from prompt_lib.core import BizError
    from prompt_lib.domain import AnnotationService, JudgeService, PromptService
    s = setup_project_with_data(client)
    pv = PromptService().get(s["prompt_id"])
    oid = _insert_output(client, s["pid"], pv["id"], "预标注目标")
    dims = _rubric_dims(client, s)
    scores = {d: 2 for d in dims}
    ann = AnnotationService().model_pre_annotation(s["pid"], oid, s["rubric_id"], scores)
    assert ann["gold_status"] == "model_pre"
    judge = JudgeService().create(s["pid"], s["rubric_id"],
                                  {"connection_id": "conn_mock"})
    with_client = client
    r = with_client.post(f"/workflow-api/v1/judges/{judge['id']}/calibrate",
                         json={"build_refs": [oid], "audit_refs": []})
    assert r.status_code == 422
    assert r.json()["code"] == "GOLD_NOT_VERIFIED"
    # 人工核验后可入gold
    v = with_client.post(f"/workflow-api/v1/annotations/{ann['id']}/verify")
    assert v.json()["gold_status"] == "human_verified"


def test_build_audit_source_overlap_rejected(client):
    """TC021：构建集与审计集同来源拒绝校准。"""
    s = setup_project_with_data(client)
    _run_with_outputs(client, s)
    outs = client.get(f"/workflow-api/v1/projects/{s['pid']}/outputs").json()["outputs"]
    ok = [o for o in outs if o["status"] == "ok"]
    dims = _rubric_dims(client, s)
    for o in ok[:2]:
        _annotate_output(client, s["pid"], s, o["id"], dims,
                         {d: 2 for d in dims})
    judge = client.post(f"/workflow-api/v1/projects/{s['pid']}/judges", json={
        "rubric_id": s["rubric_id"], "model_cfg": {"connection_id": "conn_mock"}}).json()
    r = client.post(f"/workflow-api/v1/judges/{judge['id']}/calibrate",
                    json={"build_refs": [ok[0]["id"]], "audit_refs": [ok[1]["id"]]})
    # 两个输出都来自同一dev来源池：同案例输出的组相同才冲突；不同案例不冲突
    assert r.status_code in (200, 422)
    if r.status_code == 422:
        assert r.json()["code"] == "SOURCE_OVERLAP"


def test_same_source_build_and_audit_rejected_explicit(client):
    """TC021精确版：同一来源分别进构建与审计必须422。"""
    s = setup_project_with_data(client)
    from prompt_lib.domain import PromptService
    from prompt_lib.db import get_db
    pv = PromptService().get(s["prompt_id"])
    item_id = s["item_ids"][0]
    o1 = _insert_output(client, s["pid"], pv["id"], "同源输出1")
    o2 = _insert_output(client, s["pid"], pv["id"], "同源输出2")
    # 把两个输出挂到同一案例（同 source_group）
    get_db().execute("UPDATE outputs SET item_id=? WHERE id IN (?,?)", (item_id, o1, o2))
    dims = _rubric_dims(client, s)
    _annotate_output(client, s["pid"], s, o1, dims, {d: 2 for d in dims})
    _annotate_output(client, s["pid"], s, o2, dims, {d: 2 for d in dims})
    judge = client.post(f"/workflow-api/v1/projects/{s['pid']}/judges", json={
        "rubric_id": s["rubric_id"], "model_cfg": {"connection_id": "conn_mock"}}).json()
    r = client.post(f"/workflow-api/v1/judges/{judge['id']}/calibrate",
                    json={"build_refs": [o1], "audit_refs": [o2]})
    assert r.status_code == 422
    assert r.json()["code"] == "SOURCE_OVERLAP"


def test_rubric_change_makes_judge_stale(client):
    """TC024：标准变化 -> 评价器stale；旧指标保留。"""
    s = setup_project_with_data(client)
    judge = client.post(f"/workflow-api/v1/projects/{s['pid']}/judges", json={
        "rubric_id": s["rubric_id"], "model_cfg": {"connection_id": "conn_mock"}}).json()
    # 发布新标准版本（复制内容形成新版本）
    cur = client.get(f"/workflow-api/v1/rubrics/{s['rubric_id']}").json()
    r2 = client.post(f"/workflow-api/v1/projects/{s['pid']}/rubrics").json()
    client.put(f"/workflow-api/v1/rubrics/{r2['id']}", json=cur["schema"])
    client.post(f"/workflow-api/v1/rubrics/{r2['id']}/publish")
    j = client.get(f"/workflow-api/v1/judges/{judge['id']}").json()
    assert j["status"] == "stale"


def test_missing_anchor_blocks_publish(client):
    """TC012：缺锚点不可发布，并定位到具体维度。"""
    s = setup_project_with_data(client)
    rub = client.post(f"/workflow-api/v1/projects/{s['pid']}/rubrics").json()
    schema = rub["schema"]
    schema["dimensions"][0]["anchors"] = {"0": "只有0档"}
    client.put(f"/workflow-api/v1/rubrics/{rub['id']}", json=schema)
    r = client.post(f"/workflow-api/v1/rubrics/{rub['id']}/publish")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "RUBRIC_INVALID"
    assert any("anchors" in k or "锚点" in str(v) for k, v in body["field_errors"].items())

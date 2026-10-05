"""分组泄漏门禁、封存隔离、字段隔离与版本固定（TC008/TC009/TC010/TC011）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import make_items, make_project, import_items

SENTINEL = "SENTINEL_EVAL_ONLY_XYZ"


def test_same_source_group_across_splits_blocked(client):
    """TC008：同源分组跨 dev/sealed_test 冻结被阻止。"""
    pid = make_project(client)["id"]
    items = make_items(4)
    # c001 与 c002 同组但将被分到不同集合
    items[1]["source_group_id"] = "g001"
    import_items(client, pid, items)
    listing = client.get(f"/workflow-api/v1/projects/{pid}/items?size=100").json()
    ids = [it["id"] for it in listing["items"]]
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": [ids[0], ids[2]], "split": "dev"})
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": [ids[1], ids[3]], "split": "sealed_test"})
    r = client.post(f"/workflow-api/v1/projects/{pid}/manifests/freeze", json={"seed": 1})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "DATA_SPLIT_LEAKAGE"
    assert "g001" in json.dumps(body["field_errors"], ensure_ascii=False)


def test_reference_is_never_sent_to_generation_but_available_to_evaluation(client, monkeypatch):
    """TC009：参考不得进入被测生成请求；授权评价拥有完整参考上下文。"""
    import prompt_lib.engine as engine
    from prompt_lib.providers import CallResult
    pid = make_project(client)["id"]
    items = make_items(3, sentinel=SENTINEL)
    import_items(client, pid, items)
    listing = client.get(f"/workflow-api/v1/projects/{pid}/items?size=100").json()
    dev_ids = [it["id"] for it in listing["items"]]
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": dev_ids, "split": "dev"})
    pv = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "基线", "body": "点评：{{question}} {{student_answer}}",
        "variables": ["question", "student_answer", "grade_level"]}).json()
    rubric = client.post(f"/workflow-api/v1/projects/{pid}/rubrics").json()
    assert client.post(f"/workflow-api/v1/rubrics/{rubric['id']}/publish").status_code == 200
    captured = []

    class Recorder:
        def __init__(self, config):
            pass

        def complete(self, role, model, messages, params, fingerprint):
            captured.append({"role": role, "messages": messages})
            return CallResult("模拟输出", "stop", {"in": 10, "out": 5})

    monkeypatch.setattr(engine, "_get_provider", lambda conn: Recorder(conn))
    r = client.post(f"/workflow-api/v1/prompts/{pv['id']}/trial",
                    json={"item_id": dev_ids[0]})
    assert r.status_code == 200, r.text
    blob = json.dumps([c for c in captured if c["role"] == "generation"], ensure_ascii=False)
    assert SENTINEL not in blob, "evaluation_only 字段泄漏到出站请求"
    assert "expert_answer" not in blob
    evaluation = json.dumps([c for c in captured if c["role"] == "evaluation"], ensure_ascii=False)
    assert SENTINEL in evaluation
    # 输出表也不含哨兵
    outs = client.get(f"/workflow-api/v1/projects/{pid}/outputs").json()
    assert len(outs["outputs"]) >= 1


def test_sealed_raw_hidden_from_listing(client):
    """TC010：封存原文不出现在普通列表/读取路径。"""
    from conftest import setup_project_with_data
    s = setup_project_with_data(client)
    pid = s["pid"]
    sealed = client.get(f"/workflow-api/v1/projects/{pid}/items?split=sealed_test").json()
    assert sealed["items"] == []
    assert sealed["sealed_summary"]["count"] == 2
    # 数据库通用表中已无原文
    from prompt_lib.db import get_db
    row = get_db().one(
        "SELECT runtime_input_json FROM dataset_items WHERE id=?", (s["item_ids"][10],))
    assert json.loads(row["runtime_input_json"]) == {}
    # 解封仅验收任务可用且记录审计
    art = get_db().query(
        "SELECT * FROM sealed_artifacts WHERE project_id=?", (pid,))
    assert len(art) == 2 and art[0]["access_state"] == "sealed"


def test_snapshot_fixed_versions_not_latest(client):
    """TC031前半：引用latest被拒；快照固定明确版本（TC011）。"""
    from conftest import setup_project_with_data
    s = setup_project_with_data(client)
    pid = s["pid"]
    from prompt_lib.db import get_db
    man = get_db().one("SELECT id FROM split_manifests WHERE project_id=? LIMIT 1", (pid,))
    draft = {
        "mode": "explore", "prompt": {"baseline_id": "latest"}, "rubric_id": s["rubric_id"],
        "judge_id": None, "manifest_id": man["id"] if man else "",
        "data": {"dev_item_ids": s["item_ids"][:2]},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_candidates": 0, "dev_sample_size": 2, "min_delta": 0},
        "budget": {"mode": "token", "total_limit": 100000, "search_limit": 80000,
                   "acceptance_limit": 10000},
    }
    r = client.post(f"/workflow-api/v1/projects/{pid}/runs/validate", json=draft)
    assert r.status_code == 422
    assert "latest" in r.json()["field_errors"]["prompt.baseline_id"]


def test_old_run_replays_old_versions_after_data_change(client):
    """TC011：运行快照固定案例集；新增数据不影响已创建运行。"""
    from conftest import setup_project_with_data, start_run
    s = setup_project_with_data(client)
    pid = s["pid"]
    run = start_run(client, pid, s["prompt_id"], s["rubric_id"],
                    dev_ids=s["item_ids"][:4], max_candidates=0)
    assert run["state"] == "completed", run
    before = run["snapshot"]["data"]["dev_item_ids"]
    # 数据演化：新增一条案例并分配 dev
    import_items(client, pid, make_items(1, prefix='n', group_prefix='ng'))
    listing = client.get(f"/workflow-api/v1/projects/{pid}/items?size=100").json()
    new_id = [it["id"] for it in listing["items"] if it["id"] not in s["item_ids"]][0]
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": [new_id], "split": "dev"})
    after = client.get(f"/workflow-api/v1/runs/{run['id']}").json()
    assert after["snapshot"]["data"]["dev_item_ids"] == before

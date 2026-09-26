"""测试夹具：每个用例使用独立临时数据库与应用实例。"""
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from prompt_lib.db import DB, set_db
    db = DB(str(tmp_path / "test.db"))
    set_db(db)
    from prompt_lib.api import create_app
    app = create_app(str(REPO / "web"))
    with TestClient(app) as c:
        yield c


def make_project(client, name="测试项目", task="student_feedback"):
    r = client.post("/workflow-api/v1/projects",
                    json={"name": name, "description": "测试", "task_type": task})
    assert r.status_code == 201, r.text
    return r.json()


def make_items(n=12, sentinel=None, prefix="c", group_prefix="g"):
    items = []
    for i in range(1, n + 1):
        it = {"case_id": f"{prefix}{i:03d}", "source_group_id": f"{group_prefix}{i:03d}",
              "origin": "real",
              "runtime_input": {"question": f"题目{i}", "student_answer": f"学员答案{i}",
                                "grade_level": "初中"},
              "evaluation_only": {"expert_answer": sentinel or f"参考{i}"}}
        items.append(it)
    return items


def import_items(client, pid, items):
    content = "\n".join(json.dumps(it, ensure_ascii=False) for it in items)
    r = client.post("/workflow-api/v1/projects/" + pid + "/imports/preview",
                    json={"fmt": "jsonl", "content": content})
    assert r.status_code == 200, r.text
    batch = r.json()
    r = client.post(f"/workflow-api/v1/projects/{pid}/imports/{batch['id']}/commit",
                    json={"exclude_case_ids": []})
    assert r.status_code == 200, r.text
    return batch


def setup_project_with_data(client, n=12):
    """项目 + 导入 + 分配 dev(8)/select(2)/sealed_test(2) + 冻结 + 已发布标准 + 基线提示词。"""
    pid = make_project(client)["id"]
    import_items(client, pid, make_items(n))
    listing = client.get(f"/workflow-api/v1/projects/{pid}/items?size=100").json()
    ids = [it["id"] for it in listing["items"]]
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": ids[:8], "split": "dev"})
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": ids[8:10], "split": "select"})
    client.post(f"/workflow-api/v1/projects/{pid}/split",
                json={"case_ids": ids[10:], "split": "sealed_test"})
    fr = client.post(f"/workflow-api/v1/projects/{pid}/manifests/freeze", json={"seed": 42})
    assert fr.status_code == 200, fr.text
    rub = client.post(f"/workflow-api/v1/projects/{pid}/rubrics").json()
    pub = client.post(f"/workflow-api/v1/rubrics/{rub['id']}/publish")
    assert pub.status_code == 200, pub.text
    pv = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "基线", "body": "你是一名教研老师。请点评学员答案。",
        "variables": ["question", "student_answer", "grade_level"],
        "frozen_segments": [{"name": "安全声明", "text": "不得虚构学员错误。"}], "params": {}})
    assert pv.status_code == 201, pv.text
    return {"pid": pid, "item_ids": ids, "rubric_id": rub["id"], "prompt_id": pv.json()["id"]}


def start_run(client, pid, prompt_id, rubric_id, max_candidates=2, budget=None, mode="explore",
              dev_ids=None, wait=True, idem_key=None):
    mans = client.get(f"/workflow-api/v1/projects/{pid}/manifests").json()["manifests"]
    draft = {
        "mode": mode,
        "prompt": {"baseline_id": prompt_id},
        "rubric_id": rubric_id,
        "judge_id": None,
        "manifest_id": mans[0]["id"] if mans else "",
        "data": {"dev_item_ids": dev_ids or [], "select_item_ids": []},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_candidates": max_candidates, "dev_sample_size": 6,
                         "min_delta": 0.02},
        "budget": budget or {"mode": "token", "total_limit": 10_000_000,
                             "search_limit": 8_000_000, "acceptance_limit": 1_000_000},
    }
    headers = {"Idempotency-Key": idem_key} if idem_key else {}
    r = client.post(f"/workflow-api/v1/projects/{pid}/runs", json=draft, headers=headers)
    if r.status_code != 202:
        return r
    run = r.json()
    if wait:
        run = wait_run(client, run["id"])
    return run


def wait_run(client, rid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = client.get(f"/workflow-api/v1/runs/{rid}").json()
        if run["state"] in ("completed", "failed", "cancelled", "paused_budget"):
            return run
        time.sleep(0.2)
    raise TimeoutError(f"run {rid} not settled: {run['state']}")

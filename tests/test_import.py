"""导入校验与幂等（TC006/TC007）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import make_project

GOOD = ('{"case_id":"c001","runtime_input":{"question":"题目1","student_answer":"答案1",'
        '"grade_level":"初中"},"evaluation_only":{"expert_answer":"参1"}}')


def test_preview_reports_errors_per_line_no_silent_drop(client):
    pid = make_project(client)["id"]
    overlong = '{"case_id":"c005","runtime_input":{"question":"超长","student_answer":"' + \
        "字" * 20001 + '","grade_level":"初中"}}'
    content = "\n".join([
        GOOD,
        "{坏JSON",
        '{"case_id":"c001","runtime_input":{"question":"重复ID","student_answer":"x","grade_level":"初中"}}',
        '{"case_id":"c003","runtime_input":{"question":"缺学员答案","grade_level":"初中"}}',
        '{"case_id":"c004","runtime_input":{"question":"未知字段","student_answer":"x","grade_level":"初中","hack":"注入"},'
        '"origin":"synthetic"}',
        overlong,
    ])
    r = client.post("/workflow-api/v1/projects/" + pid + "/imports/preview",
                    json={"fmt": "jsonl", "content": content})
    assert r.status_code == 200
    b = r.json()
    assert b["total"] == 5   # 坏JSON行解析失败，不计入已解析行
    assert b["valid"] == 1
    reasons = " ".join(str(e) for e in b["errors"])
    assert "JSON解析失败" in reasons
    assert "重复ID" in reasons
    assert "缺少必填运行时字段" in reasons
    assert "不在契约白名单" in reasons


def test_commit_idempotent_same_preview_hash(client):
    pid = make_project(client)["id"]
    content = GOOD + "\n" + GOOD.replace("c001", "c002")
    b1 = client.post("/workflow-api/v1/projects/" + pid + "/imports/preview",
                     json={"fmt": "jsonl", "content": content}).json()
    r1 = client.post(f"/workflow-api/v1/projects/{pid}/imports/{b1['id']}/commit",
                     json={"exclude_case_ids": []})
    assert r1.status_code == 200
    # 同一预览重复提交：幂等返回原批次，不重复建案例
    r2 = client.post(f"/workflow-api/v1/projects/{pid}/imports/{b1['id']}/commit",
                     json={"exclude_case_ids": []})
    assert r2.status_code == 200
    assert r2.json()["id"] == b1["id"]
    items = client.get(f"/workflow-api/v1/projects/{pid}/items?size=100").json()
    assert items["total"] == 2


def test_source_changed_after_preview_rejected(client):
    """预览后源内容变化 -> hash不匹配，提交被拒（TC007后半）。"""
    pid = make_project(client)["id"]
    b = client.post("/workflow-api/v1/projects/" + pid + "/imports/preview",
                    json={"fmt": "jsonl", "content": GOOD}).json()
    # 篡改受限区里的源内容，模拟"预览后文件被修改"
    from prompt_lib.db import get_db
    get_db().execute("UPDATE settings SET value_json=? WHERE key=?",
                     ('{"preview_hash":"deadbeef","valid":[]}', f"src:{b['id']}"))
    r = client.post(f"/workflow-api/v1/projects/{pid}/imports/{b['id']}/commit",
                    json={"exclude_case_ids": []})
    assert r.status_code == 409
    assert r.json()["code"] == "PREVIEW_HASH_MISMATCH"

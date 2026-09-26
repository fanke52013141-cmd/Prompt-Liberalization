"""提示词冻结段与版本库不变量（TC025/TC027）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import make_project


def test_frozen_component_change_rejected_before_any_call(client):
    """TC027：候选改冻结段在发出收费请求前被拒。"""
    from prompt_lib.core import BizError
    from prompt_lib.domain import PromptService
    pid = make_project(client)["id"]
    svc = PromptService()
    base = svc.create_version(pid, "基线", "正文", 
                              [{"name": "安全声明", "text": "不得虚构学员错误。"}],
                              ["question"], {})
    cand = dict(base)
    cand["frozen_segments"] = [{"name": "安全声明", "text": "被篡改的冻结段"}]
    try:
        svc.validate_candidate(base, cand)
        raise AssertionError("应拒绝冻结段变更")
    except BizError as e:
        assert e.code == "FROZEN_COMPONENT_CHANGED"


def test_variable_whitelist_enforced(client):
    """BR01/TC027：模板变量必须属于运行时白名单；evaluation字段禁止。"""
    pid = make_project(client)["id"]
    r = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "非法变量", "body": "{{expert_answer}}",
        "variables": ["expert_answer"]})
    assert r.status_code == 422
    assert r.json()["code"] == "VARIABLE_NOT_ALLOWED"


def test_new_version_does_not_change_old(client):
    """TC025：新增版本不改变旧版本（冻结不可变 BR02）。"""
    pid = make_project(client)["id"]
    v1 = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "P", "body": "第一版", "variables": ["question"]}).json()
    v2 = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "P", "body": "第二版", "variables": ["question"]}).json()
    assert v2["version_no"] == v1["version_no"] + 1
    v1_again = client.get(f"/workflow-api/v1/prompts/{v1['id']}").json()
    assert v1_again["body"] == "第一版"
    assert v1_again["hash"] == v1["hash"]
    assert v2["hash"] != v1["hash"]


def test_prompts_and_frozen_hashes_recorded(client):
    pid = make_project(client)["id"]
    r = client.post(f"/workflow-api/v1/projects/{pid}/prompts", json={
        "name": "P", "body": "x", "variables": ["question"],
        "frozen_segments": [{"name": "A", "text": "冻结内容"}]})
    pv = r.json()
    assert pv["frozen_segments"][0]["text"] == "冻结内容"
    assert len(pv["hash"]) == 64

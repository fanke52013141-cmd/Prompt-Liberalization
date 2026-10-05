"""Publishing must enforce all report gates, not just a positive summary."""
import json

from .test_release import _insert_report, _setup


def test_sample_gate_blocks_a_positive_report(client):
    s = _setup(client)
    rep = _insert_report(s["pid"], s["prompt_id"], "verified_improvement")
    from prompt_lib.db import get_db
    get_db().execute(
        "UPDATE acceptance_reports SET evidence_status='有效', eligibility='暂缓正式采用',"
        " gates_json=? WHERE id=?", (json.dumps({
            "严重错误门槛": {"result": "通过"},
            "样本充足门槛": {"result": "不足"},
            "证据有效性": {"result": "有效"},
        }, ensure_ascii=False), rep))
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": rep, "mode": "active"})
    assert result.status_code == 422
    assert result.json()["code"] == "REPORT_NOT_ELIGIBLE"


def test_legacy_positive_report_without_gates_cannot_be_newly_adopted(client):
    s = _setup(client)
    rep = _insert_report(s["pid"], s["prompt_id"], "verified_improvement")
    from prompt_lib.db import get_db
    get_db().execute("UPDATE acceptance_reports SET gates_json='{}', evidence_status='',"
                     "eligibility='' WHERE id=?", (rep,))
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": rep, "mode": "active"})
    assert result.status_code == 422


def test_candidate_hash_tampering_cannot_use_positive_report(client):
    s = _setup(client)
    rep = _insert_report(s["pid"], s["prompt_id"], "verified_improvement")
    from prompt_lib.db import get_db
    get_db().execute("UPDATE acceptance_reports SET candidate_hash='changed' WHERE id=?", (rep,))
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": rep, "mode": "active"})
    assert result.status_code == 422


def test_invalid_release_mode_is_rejected(client):
    s = _setup(client)
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "mode": "pretend_verified"})
    assert result.status_code == 422


def test_unadmitted_judge_blocks_otherwise_positive_report(client):
    from prompt_lib.db import get_db
    s = _setup(client)
    rep = _insert_report(s["pid"], s["prompt_id"], "verified_improvement")
    gates = json.loads(get_db().one("SELECT gates_json FROM acceptance_reports WHERE id=?", (rep,))["gates_json"])
    gates["评价器准入"] = {"result": "需人工复核"}
    get_db().execute("UPDATE acceptance_reports SET gates_json=? WHERE id=?", (json.dumps(gates), rep))
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": rep, "mode": "active"})
    assert result.status_code == 422
    assert result.json()["code"] == "REPORT_NOT_ELIGIBLE"

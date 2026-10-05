"""发布指针/试用边界/回滚/反馈/归档（TC003/TC048/TC049/TC050）。"""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import make_project


@pytest.fixture(autouse=True)
def isolate_severity_gate_for_release_pointer_tests(monkeypatch):
    # These tests exercise release pointers/configuration. Real audit evidence
    # is exercised by test_severity_audit_execution and human_acceptance.
    monkeypatch.setattr('prompt_lib.judge_binding.severe_admitted_for',lambda *args:True)


def _insert_report(pid, prompt_id, decision):
    """直接构造验收报告行（完整验收流程由e2e覆盖）。"""
    from prompt_lib.core import canonical_hash, new_id, now_iso
    from prompt_lib.domain import PromptService
    from prompt_lib.db import get_db
    rid = new_id("rep")
    run_id = new_id("run_fixture")
    from prompt_lib.domain import JudgeService
    from prompt_lib.judge_binding import evaluator_binding
    rubric=get_db().one("SELECT id FROM rubrics WHERE project_id=? AND status='published'",(pid,))
    config={'connection_id':'conn_mock','model':'mock-gen-1'}
    judge=JudgeService(get_db()).create(pid,rubric['id'],config)
    binding=evaluator_binding(get_db(),rubric['id'],config)
    get_db().execute("UPDATE judges SET status='audited',metrics_json=? WHERE id=?",(
        json.dumps({'admission':{'eligible':True},'evaluator_binding':binding}),judge['id']))
    snapshot = {"models": {"generation": {"connection_id": "conn_mock", "model": "mock-gen-1",
                "connection_snapshot": {"id": "conn_mock", "provider": "mock", "model": "mock-gen-1"}}}}
    snapshot['rubric_id']=rubric['id']
    snapshot['models']['evaluation']=config
    get_db().execute("INSERT INTO runs(id,project_id,snapshot_json,snapshot_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (run_id, pid, json.dumps(snapshot), canonical_hash(snapshot), now_iso(), now_iso()))
    get_db().execute(
        "INSERT INTO acceptance_reports(id,run_id,project_id,candidate_ref,baseline_ref,"
        "test_manifest_json,policy_json,stats_json,decision,consumed,created_at,"
        "candidate_hash,policy_hash,evidence_status,gates_json,eligibility)"
        " VALUES(?,?,?,?,?,?,?,?,?, '1', ?,?,?,?,?,?)",
        (rid, run_id, pid, prompt_id, "pv_base", "{}", "{}",
         json.dumps({"diff": 0.2,'evaluator_evidence':{'source':'model','judge_id':judge['id'],'binding':binding}}), decision, now_iso(),
         PromptService(get_db()).get(prompt_id)["hash"], canonical_hash({}), "有效",
         json.dumps({"严重错误门槛": {"result": "通过"},
                     "评价器准入": {"result": "通过"},
                     "样本充足门槛": {"result": "通过"},
                     "证据有效性": {"result": "有效"}}, ensure_ascii=False), "可正式采用"))
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


def test_active_uses_verified_configuration_and_rejects_missing_execution(client):
    from prompt_lib.db import get_db
    s = _setup(client)
    report = _insert_report(s["pid"], s["prompt_id"], "verified_improvement")
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": report, "mode": "active",
        "model_config": {"connection_id": "different", "model": "unverified-model"}})
    assert result.status_code == 201, result.text
    assert result.json()["model_config"]["model"] == "mock-gen-1"
    run_id = get_db().one("SELECT run_id FROM acceptance_reports WHERE id=?", (report,))["run_id"]
    get_db().execute("DELETE FROM runs WHERE id=?", (run_id,))
    result = client.post(f"/workflow-api/v1/projects/{s['pid']}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": report, "mode": "active"})
    assert result.status_code == 422
    assert result.json()["code"] == "REPORT_EXECUTION_MISSING"


def test_model_report_without_live_calibration_cannot_be_published(client):
    from prompt_lib.db import get_db
    s=_setup(client)
    report=_insert_report(s['pid'],s['prompt_id'],'verified_improvement')
    db=get_db()
    db.execute('UPDATE acceptance_reports SET stats_json=? WHERE id=?',
        (json.dumps({'evaluator_evidence':{'source':'model','judge_id':'missing','binding':{'hash':'old'}}}),report))
    response=client.post(f"/workflow-api/v1/projects/{s['pid']}/releases",json={
        'prompt_version_id':s['prompt_id'],'report_ref':report,'mode':'active'})
    assert response.status_code==409,response.text
    assert response.json()['code']=='CALIBRATION_CONTEXT_CHANGED'
    assert db.one('SELECT COUNT(*) FROM releases')[0]==0


def test_legacy_report_without_evaluation_source_requires_new_acceptance(client):
    from prompt_lib.db import get_db
    s=_setup(client)
    report=_insert_report(s['pid'],s['prompt_id'],'verified_improvement')
    get_db().execute("UPDATE acceptance_reports SET stats_json='{}' WHERE id=?",(report,))
    response=client.post(f"/workflow-api/v1/projects/{s['pid']}/releases",json={
        'prompt_version_id':s['prompt_id'],'report_ref':report,'mode':'active'})
    assert response.status_code==422,response.text
    assert response.json()['code']=='REPORT_EVALUATOR_MISSING'
    assert get_db().one('SELECT COUNT(*) FROM releases')[0]==0


def test_failed_new_release_insert_preserves_previous_active_pointer(client):
    from prompt_lib.db import get_db
    s=_setup(client)
    report=_insert_report(s['pid'],s['prompt_id'],'verified_improvement')
    endpoint=f"/workflow-api/v1/projects/{s['pid']}/releases"
    first=client.post(endpoint,json={'prompt_version_id':s['prompt_id'],'report_ref':report,'mode':'active'})
    assert first.status_code==201,first.text
    db=get_db()
    db.execute("CREATE TRIGGER fail_release_insert BEFORE INSERT ON releases BEGIN SELECT RAISE(ABORT,'fixture release failure'); END")
    import sqlite3
    from prompt_lib.runs import ReleaseService
    with pytest.raises(sqlite3.IntegrityError):
        ReleaseService(db).adopt(s['pid'],s['prompt_id'],report)
    assert ReleaseService(db).current(s['pid'])['id']==first.json()['id']
    assert db.one('SELECT COUNT(*) FROM releases')[0]==1


def test_release_validation_excludes_concurrent_database_writer(client,monkeypatch):
    import sqlite3
    from prompt_lib.db import get_db
    from prompt_lib.domain import PromptService
    s=_setup(client)
    report=_insert_report(s['pid'],s['prompt_id'],'verified_improvement')
    db=get_db()
    original=PromptService.get
    attempts=[]
    def checked(service,version_id):
        other=sqlite3.connect(str(db.path),timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                other.execute("UPDATE acceptance_reports SET decision='no_improvement' WHERE id=?",(report,))
            attempts.append(True)
        finally:
            other.close()
        return original(service,version_id)
    monkeypatch.setattr(PromptService,'get',checked)
    result=client.post(f"/workflow-api/v1/projects/{s['pid']}/releases",json={
        'prompt_version_id':s['prompt_id'],'report_ref':report,'mode':'active'})
    assert result.status_code==201,result.text
    assert attempts==[True]


@pytest.mark.parametrize('failure',['status','audit'])
def test_rollback_failure_restores_all_release_state(client,failure):
    import sqlite3
    from prompt_lib.db import get_db
    from prompt_lib.runs import ReleaseService
    s=_setup(client)
    db=get_db()
    service=ReleaseService(db)
    first=service.adopt(s['pid'],s['prompt_id'],_insert_report(s['pid'],s['prompt_id'],'verified_improvement'))
    second=service.adopt(s['pid'],s['prompt2_id'],_insert_report(s['pid'],s['prompt2_id'],'verified_improvement'))
    before=service.history(s['pid'])
    if failure=='status':
        db.execute("CREATE TRIGGER fail_rollback BEFORE UPDATE OF status ON releases WHEN NEW.status='rolled_back' BEGIN SELECT RAISE(ABORT,'fixture rollback failure'); END")
    else:
        db.execute("CREATE TRIGGER fail_rollback BEFORE INSERT ON audit_log WHEN NEW.action='release.rollback' BEGIN SELECT RAISE(ABORT,'fixture rollback failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.rollback(second['id'],first['id'])
    assert service.history(s['pid'])==before
    assert service.current(s['pid'])['id']==second['id']
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='release.rollback'")[0]==0
    db.execute('DROP TRIGGER fail_rollback')
    restored=service.rollback(second['id'],first['id'])
    assert restored['prompt_version_id']==first['prompt_version_id']
    assert service.get(second['id'])['status']=='rolled_back'
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='release.rollback'")[0]==1


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


def test_rollback_cannot_upgrade_trial_or_reuse_invalid_report(client):
    from prompt_lib.db import get_db
    s = _setup(client)
    pid = s["pid"]
    report = _insert_report(pid, s["prompt_id"], "verified_improvement")
    current = client.post(f"/workflow-api/v1/projects/{pid}/releases", json={
        "prompt_version_id": s["prompt_id"], "report_ref": report, "mode": "active"}).json()
    trial = client.post(f"/workflow-api/v1/projects/{pid}/releases", json={
        "prompt_version_id": s["prompt2_id"], "mode": "trial"}).json()
    result = client.post(f"/workflow-api/v1/releases/{current['id']}/rollback", json={"target_release_id": trial["id"]})
    assert result.status_code == 422
    assert result.json()["code"] == "REPORT_NOT_VERIFIED"
    get_db().execute("UPDATE acceptance_reports SET candidate_hash='tampered' WHERE id=?", (report,))
    result = client.post(f"/workflow-api/v1/releases/{current['id']}/rollback", json={"target_release_id": current["id"]})
    assert result.status_code == 422
    assert client.get(f"/workflow-api/v1/projects/{pid}/releases").json()["current"]["id"] == current["id"]


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

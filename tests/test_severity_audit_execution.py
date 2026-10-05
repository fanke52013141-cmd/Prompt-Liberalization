import json
import sqlite3

import pytest

from .test_severity_audit_ledger import create, current
from prompt_lib import engine
from prompt_lib.db import get_db
from prompt_lib.providers import ProviderError
from prompt_lib.severity_audits import SeverityAudits


def execute(client, task):
    return client.post(f"/workflow-api/v1/severity-audits/{task['id']}/execute")


def receipts(task):
    return get_db().query('SELECT * FROM ledger WHERE run_id=? ORDER BY rowid',(task['id'],))


def test_completed_audit_is_durable_idempotent_and_does_not_grant_admission(client):
    task,_,_=create(client)
    response=execute(client,task)
    assert response.status_code==200,response.text
    completed=response.json()
    assert completed['state']=='completed'
    assert len(receipts(task))==2
    assert completed['metrics']['audit']['eligible'] is False
    assert len(completed['metrics']['records']['audit'])==1
    again=execute(client,task).json()
    assert again['idempotent'] and again['metrics']==completed['metrics']
    assert len(receipts(task))==2
    metrics=json.loads(get_db().one('SELECT metrics_json FROM judges WHERE id=?',(task['judge_id'],))[0])
    assert metrics['admission']['severe_admission'] is False


def test_lost_response_pauses_without_automatic_retry_and_resumes_original_receipt(client,monkeypatch):
    task,_,_=create(client)
    original=engine._get_provider
    captured=[]

    class LostResponse:
        def __init__(self, config): self.provider=original(config)
        def complete(self,*args):
            result=self.provider.complete(*args)
            captured.append(result)
            if len(captured)==1:
                raise ProviderError('NETWORK','owned lost-response fixture',retryable=True)
            return result

    monkeypatch.setattr(engine,'_get_provider',LostResponse)
    first=execute(client,task)
    assert first.status_code==409,first.text
    assert len(captured)==len(receipts(task))==1
    assert SeverityAudits(get_db()).get(task['id'])['state']=='paused_interrupted'
    assert execute(client,task).status_code==409
    assert len(captured)==1
    row=receipts(task)[0]
    saved=captured[0]
    resolution=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/ledger/{row['attempt_id']}/reconcile-response",json={
        'reason':'核对隔离供应商原响应','evidence':'隔离供应商原请求回执',
        'request_hash':row['request_hash'],'text':saved.text,'finish':saved.finish,
        'actual_in':saved.usage['in'],'actual_out':saved.usage['out']})
    assert resolution.status_code==200,resolution.text
    final=execute(client,task)
    assert final.status_code==200,final.text
    assert final.json()['state']=='completed'
    assert len(captured)==len(receipts(task))==2
    assert current(task)['reserved']['search']==0


def test_budget_exhaustion_pauses_before_dispatch(client):
    task,_,_=create(client)
    budget=current(task)
    budget['total_limit']=budget['search_limit']=1
    get_db().execute('UPDATE severity_audits SET budget_state_json=? WHERE id=?',(json.dumps(budget),task['id']))
    response=execute(client,task)
    assert response.status_code==422,response.text
    assert response.json()['code']=='BUDGET_EXHAUSTED'
    assert SeverityAudits(get_db()).get(task['id'])['state']=='paused_budget'
    assert receipts(task)==[]

    paused=SeverityAudits(get_db()).get(task['id'])
    payload={'revision':paused['revision'],'limits':{
        'total_limit':100000,'search_limit':100000,'acceptance_limit':0}}
    endpoint=f"/workflow-api/v1/severity-audits/{task['id']}/budget"
    changed=client.post(endpoint,json=payload)
    assert changed.status_code==200,changed.text
    assert changed.json()['snapshot_hash']==task['snapshot_hash']
    assert changed.json()['budget']['prices']==task['budget']['prices']
    assert client.post(endpoint,json=payload).status_code==409
    assert execute(client,task).json()['state']=='completed'
    assert len(receipts(task))==2


@pytest.mark.parametrize('limits',[
    {'total_limit':100001,'search_limit':99999,'acceptance_limit':0},
    {'total_limit':True,'search_limit':100001,'acceptance_limit':0},
    {'total_limit':100001,'search_limit':100001,'acceptance_limit':0,'prices':{}},
    {'total_limit':100000,'search_limit':100000,'acceptance_limit':0}])
def test_budget_update_rejects_decrease_invalid_and_no_change(client,limits):
    task,_,_=create(client)
    get_db().execute("UPDATE severity_audits SET state='paused_budget' WHERE id=?",(task['id'],))
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/budget",json={
        'revision':task['revision'],'limits':limits})
    assert response.status_code==422,response.text
    assert current(task)==task['budget']


def test_completion_transaction_failure_preserves_receipts_for_resume(client):
    task,_,_=create(client)
    db=get_db()
    db.execute("CREATE TRIGGER fail_severity_completion BEFORE UPDATE OF metrics_json ON judges BEGIN SELECT RAISE(ABORT,'fixture completion failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        SeverityAudits(db).execute(task['id'])
    paused=SeverityAudits(db).get(task['id'])
    assert paused['state']=='paused_interrupted' and paused['metrics']=={}
    assert len(receipts(task))==2
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.completed'")[0]==0
    db.execute('DROP TRIGGER fail_severity_completion')
    assert execute(client,task).json()['state']=='completed'
    assert len(receipts(task))==2
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.completed'")[0]==1


def test_money_increase_preserves_unknown_reservation_and_frozen_price(client):
    task,ledger,budget=create(client,'money')
    attempt=ledger.reserve(task['id'],'unknown-fixture','evaluation','mock-gen-1','search',30,
                           budget,'fixture-hash',estimated_in=10,estimated_out=20)
    ledger.mark_sent_unknown(attempt,budget)
    db=get_db()
    db.execute("UPDATE severity_audits SET state='paused_budget' WHERE id=?",(task['id'],))
    before=current(task)
    client.put('/workflow-api/v1/settings/prices',json={
        'mock-gen-1':{'in_per_1k':'99','out_per_1k':'99','currency':'USD'}})
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/budget",json={
        'revision':task['revision'],'limits':{'total_limit':'11','search_limit':'11','acceptance_limit':0}})
    assert response.status_code==200,response.text
    after=response.json()['budget']
    assert after['prices']==before['prices']
    assert after['spent']==before['spent'] and after['reserved']==before['reserved']
    assert receipts(task)[0]['status']=='sent_unknown'
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.budget_increased'")[0]==1


def test_cancel_during_response_saves_receipt_and_prevents_next_call(client,monkeypatch):
    task,_,_=create(client)
    original=engine._get_provider
    calls=[]
    service=SeverityAudits(get_db())

    class CancelDuringResponse:
        def __init__(self, config): self.provider=original(config)
        def complete(self,*args):
            calls.append(args)
            current_task=service.get(task['id'])
            service.cancel(task['id'],{'revision':current_task['revision'],'reason':'隔离并发停止'})
            return self.provider.complete(*args)

    monkeypatch.setattr(engine,'_get_provider',CancelDuringResponse)
    response=execute(client,task)
    assert response.status_code==200,response.text
    assert response.json()['state']=='cancelled'
    assert len(calls)==len(receipts(task))==1
    assert receipts(task)[0]['status']=='ok'
    assert current(task)['spent']['search']>0
    assert execute(client,task).status_code==409
    assert service.cancel(task['id'],{'revision':0,'reason':'重发停止'})['idempotent']


def test_cancel_keeps_unknown_cost_and_blocks_ledger_precharge(client):
    from prompt_lib.core import BizError
    task,ledger,budget=create(client)
    attempt=ledger.reserve(task['id'],'unknown','evaluation','mock-gen-1','search',30,budget,'hash')
    ledger.mark_sent_unknown(attempt,budget)
    before=current(task)
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/cancel",json={
        'revision':task['revision'],'reason':'保留未知费用后停止'})
    assert response.status_code==200,response.text
    assert current(task)==before
    with pytest.raises(BizError) as error:
        ledger.reserve(task['id'],'next','evaluation','mock-gen-1','search',30,budget,'next-hash')
    assert error.value.code=='AUDIT_STATE_INVALID'
    assert len(receipts(task))==1


@pytest.mark.parametrize('tamper',['metrics','receipt','completion','gold'])
def test_evidence_verification_detects_live_changes(client,tamper):
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    endpoint=f"/workflow-api/v1/severity-audits/{task['id']}/verify-evidence"
    verified=client.post(endpoint)
    assert verified.status_code==200,verified.text
    assert verified.json()['evidence_valid'] and not verified.json()['statistically_eligible']
    db=get_db()
    if tamper=='metrics':
        db.execute("UPDATE severity_audits SET metrics_json='{}' WHERE id=?",(task['id'],))
    elif tamper=='receipt':
        db.execute("UPDATE ledger SET response_json='{}' WHERE run_id=?",(task['id'],))
    elif tamper=='completion':
        db.execute("DELETE FROM audit_log WHERE action='severity_audit.completed' AND target=?",(task['id'],))
    else:
        db.execute("UPDATE audit_log SET payload_hash='changed' WHERE action='severity_gold.confirmed'")
    response=client.post(endpoint)
    assert response.status_code==409,response.text


def test_self_consistent_modified_statistics_cannot_replace_original_prediction(client):
    from prompt_lib.core import canonical_hash
    from prompt_core.severity_calibration import audit_severity
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    db=get_db()
    saved=SeverityAudits(db).get(task['id'])['metrics']
    row=saved['records']['audit'][0]
    row['prediction']={key:True for key in row['prediction']}
    snapshot=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])
    manifest=snapshot['manifest']
    saved['audit']=audit_severity(saved['records']['audit'],list(row['prediction']),manifest['policy'],
        build_sources=[r['source_group'] for r in manifest['build']])
    db.execute('UPDATE severity_audits SET metrics_json=? WHERE id=?',(json.dumps(saved),task['id']))
    db.execute("UPDATE audit_log SET payload_hash=? WHERE action='severity_audit.completed' AND target=?",(canonical_hash(saved),task['id']))
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/verify-evidence")
    assert response.status_code==409,response.text
    assert response.json()['code']=='AUDIT_EVIDENCE_CHANGED'


def test_deterministic_quality_metric_cannot_create_model_severity_audit(client):
    from .test_severity_audits import fixture_audit
    j,body=fixture_audit(client)
    db=get_db()
    config=json.loads(db.one('SELECT model_config_json FROM judges WHERE id=?',(j['id'],))[0])
    config['metric']={'type':'exact_match','reference_field':'answer'}
    db.execute('UPDATE judges SET model_config_json=? WHERE id=?',(json.dumps(config),j['id']))
    before=db.one('SELECT COUNT(*) FROM ledger')[0]
    response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code==422,response.text
    assert response.json()['code']=='SEVERITY_EVALUATOR_INVALID'
    assert db.one('SELECT COUNT(*) FROM severity_audits')[0]==0
    assert db.one('SELECT COUNT(*) FROM ledger')[0]==before


def test_modified_request_fingerprint_is_rejected_even_with_refreshed_receipt_binding(client):
    from prompt_lib.core import canonical_hash
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    db=get_db()
    service=SeverityAudits(db)
    db.execute("UPDATE ledger SET request_hash='substituted-request' WHERE run_id=?",(task['id'],))
    metrics=service.get(task['id'])['metrics']
    metrics['receipt_binding']=service.receipt_binding(task['id'])
    db.execute('UPDATE severity_audits SET metrics_json=? WHERE id=?',(json.dumps(metrics),task['id']))
    db.execute("UPDATE audit_log SET payload_hash=? WHERE action='severity_audit.completed' AND target=?",(canonical_hash(metrics),task['id']))
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/verify-evidence")
    assert response.status_code==409,response.text
    assert response.json()['code']=='AUDIT_EVIDENCE_CHANGED'


def test_valid_but_unqualified_audit_cannot_grant_severe_admission(client):
    from prompt_lib.judge_binding import severe_admitted_for, evaluator_binding
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    db=get_db()
    snapshot=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])
    manifest=snapshot['manifest']
    config=manifest['model_config']
    judge=db.one('SELECT * FROM judges WHERE id=?',(task['judge_id'],))
    metrics=json.loads(judge['metrics_json'])
    metrics['admission']={'eligible':True,'severe_admission':True}
    metrics['evaluator_binding']=evaluator_binding(db,manifest['rubric_id'],config)
    db.execute("UPDATE judges SET status='audited',metrics_json=? WHERE id=?",(json.dumps(metrics),judge['id']))
    judge=db.one('SELECT * FROM judges WHERE id=?',(judge['id'],))
    assert SeverityAudits(db).verify_evidence(task['id'])['evidence_valid']
    assert not severe_admitted_for(db,judge,manifest['rubric_id'],config)


def test_activation_rejects_unqualified_audit_without_changing_judge(client):
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    service=SeverityAudits(get_db())
    completed=service.get(task['id'])
    before=get_db().one('SELECT metrics_json FROM judges WHERE id=?',(task['judge_id'],))[0]
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/activate",json={
        'revision':completed['revision'],'reviewer':'隔离审核人','reason':'核对独立审计'})
    assert response.status_code==422,response.text
    assert response.json()['code']=='SEVERITY_AUDIT_NOT_ELIGIBLE'
    assert get_db().one('SELECT metrics_json FROM judges WHERE id=?',(task['judge_id'],))[0]==before
    assert get_db().one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.activated'")[0]==0


def test_activation_transaction_and_idempotence_with_isolated_evidence_gate(client,monkeypatch):
    # Unit isolation of a qualifying evidence gate; real statistical admission
    # requires a separate mixed positive/negative gold end-to-end test.
    import sqlite3
    from prompt_lib.judge_binding import evaluator_binding
    task,_,_=create(client)
    assert execute(client,task).status_code==200
    db=get_db()
    service=SeverityAudits(db)
    task=service.get(task['id'])
    manifest=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])['manifest']
    monkeypatch.setattr(SeverityAudits,'verify_evidence',lambda self,aid:{
        'evidence_valid':True,'statistically_eligible':True,'receipt_binding':self.receipt_binding(aid)})
    payload={'revision':task['revision'],'reviewer':'隔离审核人','reason':'事务单元测试'}
    from prompt_lib.core import BizError
    with pytest.raises(BizError) as error:
        service.activate(task['id'],payload)
    assert error.value.code=='SCORE_CALIBRATION_REQUIRED'
    metrics={'admission':{'eligible':True,'severe_admission':False},
             'evaluator_binding':evaluator_binding(db,manifest['rubric_id'],manifest['model_config'])}
    db.execute("UPDATE judges SET status='audited',metrics_json=? WHERE id=?",(json.dumps(metrics),task['judge_id']))
    db.execute("CREATE TRIGGER fail_activation BEFORE INSERT ON audit_log WHEN NEW.action='severity_audit.activated' BEGIN SELECT RAISE(ABORT,'fixture activation failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.activate(task['id'],payload)
    assert json.loads(db.one('SELECT metrics_json FROM judges WHERE id=?',(task['judge_id'],))[0])==metrics
    db.execute('DROP TRIGGER fail_activation')
    assert service.activate(task['id'],payload)['activated']
    assert service.activate(task['id'],payload)['idempotent']
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.activated'")[0]==1

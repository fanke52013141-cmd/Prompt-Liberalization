import json
from decimal import Decimal

import pytest

from .test_severity_audits import fixture_audit
from prompt_lib.core import BizError
from prompt_lib.db import get_db
from prompt_lib.ledger import BudgetState, Ledger


def create(client, mode='token'):
    j,body=fixture_audit(client,mode)
    if mode=='money':
        client.put('/workflow-api/v1/settings/prices',json={'mock-gen-1':{'in_per_1k':'1','out_per_1k':'2','currency':'CNY'}})
    response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code==201,response.text
    task=response.json()
    return task,Ledger(get_db()),BudgetState(task['budget'])


def current(task):
    return json.loads(get_db().one('SELECT budget_state_json FROM severity_audits WHERE id=?',(task['id'],))[0])


def test_audit_budget_precharge_settlement_and_stale_caller_are_persisted(client):
    task,ledger,budget=create(client)
    saved=current(task)
    saved['total_limit']=saved['search_limit']=50
    get_db().execute('UPDATE severity_audits SET budget_state_json=? WHERE id=?',(json.dumps(saved),task['id']))
    first=ledger.reserve(task['id'],'logical-1','evaluation','mock-gen-1','search',30,budget,'hash-1')
    assert current(task)['reserved']['search']==30
    stale=BudgetState({'mode':'token','total_limit':1000000,'search_limit':1000000,'acceptance_limit':0})
    with pytest.raises(BizError) as error:
        ledger.reserve(task['id'],'logical-2','evaluation','mock-gen-1','search',30,stale,'hash-2')
    assert error.value.code=='BUDGET_EXHAUSTED'
    assert get_db().one('SELECT COUNT(*) FROM ledger WHERE run_id=?',(task['id'],))[0]==1
    ledger.settle(first,budget,5,5,response={'text':'saved','finish':'stop','usage':{'in':5,'out':5}})
    assert current(task)['spent']['search']==10 and current(task)['reserved']['search']==0
    ledger.reserve(task['id'],'logical-2','evaluation','mock-gen-1','search',30,stale,'hash-2')
    assert current(task)['spent']['search']==10 and current(task)['reserved']['search']==30


@pytest.mark.parametrize('mode',['token','money'])
def test_audit_usage_unknown_keeps_reservation_and_reconciles_once(client,mode):
    task,ledger,budget=create(client,mode)
    attempt=ledger.reserve(task['id'],'logical-1','evaluation','mock-gen-1','search',30,budget,'hash',estimated_in=10,estimated_out=20)
    before=current(task)['reserved']['search']
    ledger.mark_usage_unknown(attempt,budget,response={'text':'saved','finish':'stop','usage':{}})
    assert current(task)['reserved']['search']==before
    payload={'reason':'隔离审计供应商用量核对','evidence':'供应商用量记录','request_hash':'hash','actual_in':2,'actual_out':3}
    ledger.reconcile_usage(task['id'],attempt,payload)
    ledger.reconcile_usage(task['id'],attempt,payload)
    assert current(task)['spent']['search']==('0.008' if mode=='money' else 5)
    assert Decimal(str(current(task)['reserved']['search']))==0
    assert get_db().one("SELECT COUNT(*) FROM audit_log WHERE action='ledger.reconcile_usage' AND target=?",(attempt,))[0]==1


@pytest.mark.parametrize('resolution',['response','not_accepted'])
def test_audit_unconfirmed_call_resolves_without_losing_budget(client,resolution):
    task,ledger,budget=create(client)
    attempt=ledger.reserve(task['id'],'logical-1','evaluation','mock-gen-1','search',30,budget,'hash')
    ledger.mark_sent_unknown(attempt,budget)
    assert current(task)['reserved']['search']==30
    payload={'reason':'核对审计原请求','evidence':'供应商留存记录','request_hash':'hash'}
    if resolution=='response':
        ledger.reconcile_response(task['id'],attempt,{**payload,'text':'saved','finish':'stop','actual_in':2,'actual_out':3})
        assert current(task)['spent']['search']==5
    else:
        ledger.reconcile_not_accepted(task['id'],attempt,{**payload,'confirmed_not_accepted':True})
        assert current(task)['spent']['search']==0
    assert current(task)['reserved']['search']==0


def test_audit_reconciliation_api_is_scoped_and_public_view_omits_raw_receipt(client):
    task,ledger,budget=create(client)
    attempt=ledger.reserve(task['id'],'logical-1','evaluation','mock-gen-1','search',30,budget,'hash')
    ledger.mark_sent_unknown(attempt,budget)
    other,_,_=create(client)
    payload={'reason':'核对审计原响应','evidence':'供应商记录','request_hash':'hash',
             'text':'{"severe":false}','finish':'stop','actual_in':2,'actual_out':3}
    base=f"/workflow-api/v1/severity-audits/{task['id']}/ledger/{attempt}"
    wrong=client.post(f"/workflow-api/v1/severity-audits/{other['id']}/ledger/{attempt}/reconcile-response",json=payload)
    assert wrong.status_code==404
    assert client.post(f"/workflow-api/v1/runs/{task['id']}/ledger/{attempt}/reconcile-response",json=payload).status_code==404
    assert current(task)['reserved']['search']==30
    response=client.post(base+'/reconcile-response',json=payload)
    assert response.status_code==200,response.text
    assert client.post(base+'/reconcile-response',json=payload).status_code==200
    attempts=client.get(f"/workflow-api/v1/severity-audits/{task['id']}/ledger/attempts").json()['attempts']
    assert attempts[0]['status']=='ok'
    assert 'response_json' not in attempts[0] and 'api_key' not in attempts[0]
    assert current(task)['spent']['search']==5


def test_audit_retry_grant_keeps_old_unconfirmed_charge(client):
    task,ledger,budget=create(client)
    attempt=ledger.reserve(task['id'],'logical-1','evaluation','mock-gen-1','search',30,budget,'hash')
    ledger.mark_sent_unknown(attempt,budget)
    payload={'reason':'接受新尝试可能重复计费','evidence':'原结果仍待核实','request_hash':'hash','confirmed_possible_duplicate':True}
    response=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/ledger/{attempt}/authorize-retry",json=payload)
    assert response.status_code==200,response.text
    assert current(task)['reserved']['search']==30
    row=get_db().one('SELECT status,retry_authorization_json,retry_consumed FROM ledger WHERE attempt_id=?',(attempt,))
    assert row['status']=='sent_unknown' and row['retry_authorization_json'] and row['retry_consumed']==0


def test_failed_audit_budget_write_rolls_back_precharge_before_provider_dispatch(client,monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from prompt_lib import engine
    task,ledger,budget=create(client)
    db=get_db()
    snapshot=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])
    dispatched=[]
    monkeypatch.setattr(engine,'_get_provider',lambda config:SimpleNamespace(complete=lambda *args:dispatched.append(args)))
    db.execute("CREATE TRIGGER fail_audit_budget BEFORE UPDATE OF budget_state_json ON severity_audits BEGIN SELECT RAISE(ABORT,'interrupt audit budget write'); END")
    with pytest.raises(sqlite3.IntegrityError):
        engine.call_model(db,'evaluation',snapshot['manifest']['model_config'],[{'role':'user','content':'isolated precharge fault'}],{},
                          task['id'],'physical-test','search',budget,ledger)
    assert dispatched==[]
    assert db.one('SELECT COUNT(*) FROM ledger WHERE run_id=?',(task['id'],))[0]==0
    assert current(task)['reserved']['search']==0

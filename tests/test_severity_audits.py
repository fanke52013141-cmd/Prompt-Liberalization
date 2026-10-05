import json

import pytest

from .test_severity_gold import setup_gold, submit, context, judge
from prompt_lib.db import DB, get_db, set_db


def fixture_audit(client, mode='token'):
    data,_,outputs = setup_gold(client)
    refs = [submit(client,data,context(client,data,oid)).json()['id'] for oid in outputs]
    j = judge(data)
    budget = {'mode':mode,'total_limit':'10' if mode=='money' else 100000,
              'search_limit':'10' if mode=='money' else 100000,'acceptance_limit':0}
    return j, {'build_gold_ids':refs[:1],'audit_gold_ids':refs[1:],'budget':budget}


def test_task_is_bound_once_persisted_and_creates_no_model_call(client):
    j,body = fixture_audit(client)
    db = get_db()
    calls = db.one('SELECT COUNT(*) FROM ledger')[0]
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code == 201,response.text
    task = response.json()
    assert task['state']=='bound' and task['build_count']==task['audit_count']==1
    assert task['budget']['spent']=={'search':0,'acceptance':0}
    second = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).json()
    assert second['idempotent'] and second['id']==task['id']
    listed=client.get(f"/workflow-api/v1/judges/{j['id']}/severity-audits").json()['tasks']
    assert len(listed)==1 and listed[0]['id']==task['id']
    path=db.path
    db._conn.close()
    set_db(DB(str(path)))
    restored=client.get(f"/workflow-api/v1/severity-audits/{task['id']}").json()
    assert restored['snapshot_hash']==task['snapshot_hash']
    assert get_db().one('SELECT COUNT(*) FROM ledger')[0]==calls
    assert get_db().one("SELECT COUNT(*) FROM audit_log WHERE action='severity_audit.bound'")[0]==1


def test_money_prices_and_empty_usage_are_server_owned(client):
    j,body=fixture_audit(client,'money')
    body['budget'].update(prices={'mock-gen-1':{'in_per_1k':'0','out_per_1k':'0','currency':'USD'}},spent={'search':9,'acceptance':0})
    assert client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).status_code==422
    client.put('/workflow-api/v1/settings/prices',json={'mock-gen-1':{'in_per_1k':'1','out_per_1k':'2','currency':'CNY'}})
    task=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).json()
    assert task['budget']['prices']['mock-gen-1']['currency']=='CNY'
    assert task['budget']['spent']['search']=='0'
    client.put('/workflow-api/v1/settings/prices',json={'mock-gen-1':{'in_per_1k':'5','out_per_1k':'6','currency':'CNY'}})
    restored=client.get(f"/workflow-api/v1/severity-audits/{task['id']}").json()
    assert restored['budget']['prices']['mock-gen-1']['in_per_1k']=='1'


def test_task_snapshot_tampering_is_rejected(client):
    j,body=fixture_audit(client)
    task=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).json()
    db=get_db()
    snap=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])
    snap['manifest']['policy']['min_recall_lower']=0
    db.execute('UPDATE severity_audits SET snapshot_json=? WHERE id=?',(json.dumps(snap),task['id']))
    response=client.get(f"/workflow-api/v1/severity-audits/{task['id']}")
    assert response.status_code==409
    assert response.json()['code']=='SNAPSHOT_INTEGRITY_INVALID'


@pytest.mark.parametrize('budget',[None,{}, {'mode':'token','total_limit':True,'search_limit':1},
    {'mode':'money','total_limit':'NaN','search_limit':1}])
def test_invalid_budget_never_creates_a_task(client,budget):
    j,body=fixture_audit(client)
    body['budget']=budget
    response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code==422
    assert get_db().one('SELECT COUNT(*) FROM severity_audits')[0]==0


def test_changing_budget_cannot_create_another_task_on_same_audit_sources(client):
    j,body=fixture_audit(client)
    assert client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).status_code==201
    body['budget']['total_limit']=body['budget']['search_limit']=200000
    response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code==409
    assert response.json()['code']=='AUDIT_SOURCE_ALREADY_USED'
    assert get_db().one('SELECT COUNT(*) FROM severity_audits')[0]==1


def test_previous_construction_sources_cannot_be_reclassified_as_fresh_audit(client):
    j,body=fixture_audit(client)
    assert client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).status_code==201
    body['build_gold_ids'],body['audit_gold_ids']=body['audit_gold_ids'],body['build_gold_ids']
    response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
    assert response.status_code==409
    assert response.json()['code']=='AUDIT_SOURCE_ALREADY_USED'


def test_project_deletion_does_not_leave_orphaned_calibration_jobs(client):
    from prompt_lib.domain import ProjectsService
    j,body=fixture_audit(client)
    task=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body).json()
    ProjectsService(get_db()).delete(task['project_id'])
    assert get_db().one('SELECT COUNT(*) FROM severity_audits')[0]==0
    assert get_db().query('PRAGMA foreign_key_check')==[]

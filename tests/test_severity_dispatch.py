import threading
import time

from .test_severity_audit_ledger import create
from prompt_lib import engine
from prompt_lib.db import get_db
from prompt_lib.severity_audits import SeverityAudits


def test_background_dispatch_returns_before_provider_and_deduplicates(client,monkeypatch):
    task,_,_=create(client)
    entered=threading.Event()
    release=threading.Event()
    original=engine._get_provider
    calls=[]
    class WaitingProvider:
        def __init__(self,config): self.provider=original(config)
        def complete(self,*args):
            calls.append(args)
            entered.set()
            assert release.wait(10)
            return self.provider.complete(*args)
    monkeypatch.setattr(engine,'_get_provider',WaitingProvider)
    endpoint=f"/workflow-api/v1/severity-audits/{task['id']}/dispatch"
    try:
        response=client.post(endpoint)
        assert response.status_code==202,response.text
        assert entered.wait(5)
        repeated=client.post(endpoint)
        assert repeated.status_code==202 and repeated.json()['idempotent']
        assert len(calls)==1
    finally:
        release.set()
    service=SeverityAudits(get_db())
    deadline=time.monotonic()+10
    while service.get(task['id'])['state']=='running' and time.monotonic()<deadline:
        time.sleep(.01)
    assert service.get(task['id'])['state']=='completed'
    assert len(calls)==2


def test_background_capacity_rejects_without_occupying_or_dispatching_new_task(client,monkeypatch):
    from prompt_lib import severity_audits
    from prompt_lib.core import BizError
    import pytest
    tasks=[create(client)[0] for _ in range(2)]
    entered=threading.Event()
    release=threading.Event()
    original=engine._get_provider
    calls=[]
    class WaitingProvider:
        def __init__(self,config): self.provider=original(config)
        def complete(self,*args):
            calls.append(args)
            entered.set()
            assert release.wait(10)
            return self.provider.complete(*args)
    monkeypatch.setattr(engine,'_get_provider',WaitingProvider)
    monkeypatch.setattr(severity_audits,'_MAX_AUDIT_WORKERS',1)
    service=SeverityAudits(get_db())
    try:
        service.dispatch(tasks[0]['id'])
        assert entered.wait(5)
        assert service.dispatch(tasks[0]['id'])['idempotent']
        with pytest.raises(BizError) as synchronous_error:
            service.execute(tasks[1]['id'])
        assert synchronous_error.value.code=='AUDIT_CAPACITY_BUSY'
        with pytest.raises(BizError) as error:
            service.dispatch(tasks[1]['id'])
        assert error.value.code=='AUDIT_CAPACITY_BUSY'
        assert service.get(tasks[1]['id'])['state']=='bound'
        assert get_db().one('SELECT COUNT(*) FROM ledger WHERE run_id=?',(tasks[1]['id'],))[0]==0
    finally:
        release.set()
    deadline=time.monotonic()+10
    while any(w.is_alive() for w in severity_audits._workers.values()) and time.monotonic()<deadline:
        time.sleep(.01)
    assert service.get(tasks[0]['id'])['state']=='completed'
    service.dispatch(tasks[1]['id'])
    deadline=time.monotonic()+10
    while any(w.is_alive() for w in severity_audits._workers.values()) and time.monotonic()<deadline:
        time.sleep(.01)
    assert service.get(tasks[1]['id'])['state']=='completed'


def test_startup_recovery_keeps_audit_charge_and_never_replays_unknown_request(client,monkeypatch):
    import json
    import pytest
    from prompt_lib.core import BizError
    from prompt_lib.runs import RunService
    from prompt_lib.ledger import Ledger
    task,ledger,budget=create(client)
    db=get_db()
    service=SeverityAudits(db)
    snapshot=json.loads(db.one('SELECT snapshot_json FROM severity_audits WHERE id=?',(task['id'],))[0])
    gold=snapshot['manifest']['build'][0]
    from prompt_lib.engine import evaluation_messages,request_fingerprint,get_connection
    manifest=snapshot['manifest']
    config=manifest['model_config']
    schema=json.loads(db.one('SELECT schema_json FROM rubrics WHERE id=?',(manifest['rubric_id'],))[0])
    fingerprint=request_fingerprint('evaluation',config['model'],evaluation_messages(schema,gold['context']['text'],
        gold['context']['task']['input'],gold['context']['task']['reference']),config.get('params') or {},get_connection(config))
    attempt=ledger.reserve(task['id'],f"{task['id']}:build:{gold['gold_id']}",
                           'evaluation','mock-gen-1','search',400,budget,fingerprint)
    db.execute("UPDATE severity_audits SET state='running' WHERE id=?",(task['id'],))
    RunService(db).recover_interrupted()
    recovered=service.get(task['id'])
    assert recovered['state']=='paused_interrupted'
    assert recovered['budget']['reserved']['search']==400
    assert db.one('SELECT status FROM ledger WHERE attempt_id=?',(attempt,))[0]=='sent_unknown'
    calls=[]
    monkeypatch.setattr(engine,'_get_provider',lambda config:calls.append(config))
    with pytest.raises(BizError) as error:
        service.execute(task['id'])
    assert error.value.code=='CALL_RESULT_UNCONFIRMED'
    assert calls==[]
    assert db.one('SELECT COUNT(*) FROM ledger WHERE run_id=?',(task['id'],))[0]==1
    assert service.get(task['id'])['budget']['reserved']['search']==400
    revision=service.get(task['id'])['revision']
    RunService(db).recover_interrupted()
    assert service.get(task['id'])['revision']==revision

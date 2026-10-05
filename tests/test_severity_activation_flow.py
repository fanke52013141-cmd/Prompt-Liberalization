"""Offline provider integration; real audit statistics/evidence gates are intact."""
import json

from .conftest import setup_project_with_data, start_run
from .test_severity_gold import context, submit
from prompt_lib import engine
from prompt_lib.db import get_db
from prompt_lib.domain import JudgeService, AnnotationService
from prompt_lib.judge_binding import severe_admitted_for
from prompt_lib.providers import CallResult


def test_mixed_gold_actual_audit_activation_and_live_invalidation(client,monkeypatch):
    data=setup_project_with_data(client,n=20)
    db=get_db()
    schema=json.loads(db.one('SELECT schema_json FROM rubrics WHERE id=?',(data['rubric_id'],))[0])
    schema['severity_examples']=[{'id':'forbidden','description':'输出不能含有危险标记'}]
    db.execute('UPDATE rubrics SET schema_json=? WHERE id=?',(json.dumps(schema),data['rubric_id']))
    run=start_run(client,data['pid'],data['prompt_id'],data['rubric_id'],
                  dev_ids=data['item_ids'][:6],max_candidates=0)
    outputs=db.query('SELECT id FROM outputs WHERE run_id=? ORDER BY rowid',(run['id'],))
    assert len(outputs)==6
    refs=[]
    for index,row in enumerate(outputs):
        text='包含危险标记' if index==1 else '合格输出'
        db.execute('UPDATE outputs SET text=? WHERE id=?',(text,row['id']))
        ctx=context(client,data,row['id'])
        positive=index==1
        evidence=[{'rule_id':'forbidden','start':2,'end':6,'quote':'危险标记'}] if positive else []
        response=submit(client,data,ctx,labels={'forbidden':positive},evidence=evidence)
        assert response.status_code==201,response.text
        refs.append(response.json()['id'])
    # Permissive policy is deliberately diagnostic and must not be interpreted
    # as a business recommendation or sufficient production sample support.
    config={'connection_id':'conn_mock','calibration_policy':{
        'min_groups':5,'min_exact_rate':1,'max_abstain_rate':0},'severity_calibration_policy':{
        'min_positive_groups':1,'min_negative_groups':1,'min_recall_lower':0,
        'max_false_positive_upper':1,'max_unknown_rate':0,'confidence':.95,'simultaneous':False}}
    judge=JudgeService(db).create(data['pid'],data['rubric_id'],config)
    task=client.post(f"/workflow-api/v1/judges/{judge['id']}/severity-audits",json={
        'build_gold_ids':refs[:1],'audit_gold_ids':refs[1:],
        'budget':{'mode':'token','total_limit':100000,'search_limit':100000,'acceptance_limit':0}}).json()
    calls=[]
    class OfflineProvider:
        def complete(self,role,model,messages,params,fingerprint):
            text=messages[1]['content'].split('<output>\n')[1].split('\n</output>')[0]
            severe='危险标记' in text
            violations=[{'rule_id':'forbidden','start':text.index('危险标记'),
                         'end':text.index('危险标记')+4,'quote':'危险标记'}] if severe else []
            calls.append(fingerprint)
            return CallResult(json.dumps({'scores':{d['name']:2 for d in schema['dimensions']},
                'abstain':False,'severe':severe,'violations':violations}), 'stop',{'in':20,'out':20})
    monkeypatch.setattr(engine,'_get_provider',lambda config:OfflineProvider())
    completed=client.post(f"/workflow-api/v1/severity-audits/{task['id']}/execute")
    assert completed.status_code==200,completed.text
    assert completed.json()['metrics']['audit']['eligible'] is True
    assert len(calls)==6
    annotations=AnnotationService(db)
    for row in outputs:
        pre=annotations.model_pre_annotation(data['pid'],row['id'],data['rubric_id'],
                                             {d['name']:2 for d in schema['dimensions']})
        annotations.verify_annotation(pre['id'])
    calibrated=JudgeService(db).calibrate(judge['id'],[outputs[0]['id']],[r['id'] for r in outputs[1:]])
    assert calibrated['metrics']['admission']['eligible'] is True
    assert calibrated['metrics']['severity_reactivation_required'] is True
    assert len(calls)==12
    endpoint=f"/workflow-api/v1/severity-audits/{task['id']}/activate"
    body={'revision':completed.json()['revision'],'reviewer':'隔离审核人','reason':'验证正负金标完整流程'}
    activated=client.post(endpoint,json=body)
    assert activated.status_code==200,activated.text
    assert client.post(endpoint,json=body).json()['idempotent']
    current=db.one('SELECT * FROM judges WHERE id=?',(judge['id'],))
    assert severe_admitted_for(db,current,data['rubric_id'],config)
    db.execute("UPDATE outputs SET text='改变的原始输出' WHERE id=?",(outputs[1]['id'],))
    assert not severe_admitted_for(db,current,data['rubric_id'],config)
    assert client.post(endpoint,json=body).status_code==409
    assert len(calls)==12

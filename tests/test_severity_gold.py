import json

import pytest

from .conftest import setup_project_with_data, start_run, make_project
from prompt_lib.db import get_db
from prompt_lib.domain import JudgeService


POLICY = {'min_positive_groups':1, 'min_negative_groups':1, 'min_recall_lower':.8,
          'max_false_positive_upper':.1, 'max_unknown_rate':.1, 'confidence':.95, 'simultaneous':False}


def setup_gold(client):
    data = setup_project_with_data(client)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=0)
    outputs = get_db().query('SELECT id FROM outputs WHERE run_id=? ORDER BY rowid', (run['id'],))
    return data, run, [r['id'] for r in outputs]


def context(client, data, oid):
    response = client.get(f"/workflow-api/v1/projects/{data['pid']}/severity-gold/context",
                          params={'output_id':oid,'rubric_id':data['rubric_id']})
    assert response.status_code == 200, response.text
    return response.json()


def payload(ctx, **overrides):
    return {k:ctx[k] for k in ('output_id','rubric_id','output_hash','rubric_hash','case_hash')} | {
        'confirmed_human_review':True, 'labels':{r:False for r in ctx['rules']},
        'reviewer':'隔离人工金标评审', 'reason':'逐规则核对输入参考及完整输出', 'evidence':[], **overrides}


def submit(client, data, ctx, **overrides):
    return client.post(f"/workflow-api/v1/projects/{data['pid']}/severity-gold", json=payload(ctx, **overrides))


def judge(data):
    return JudgeService(get_db()).create(data['pid'], data['rubric_id'],
        {'connection_id':'conn_mock','severity_calibration_policy':POLICY})


def test_gold_is_immutable_auditable_and_preflight_does_not_call_models(client):
    data, run, outputs = setup_gold(client)
    refs = []
    for oid in outputs:
        ctx = context(client,data,oid)
        response = submit(client,data,ctx)
        assert response.status_code == 201, response.text
        refs.append(response.json()['id'])
        assert submit(client,data,ctx).json()['idempotent']
        assert submit(client,data,ctx,reason='看过结果再改判').status_code == 409
    db = get_db()
    calls = db.one('SELECT COUNT(*) FROM ledger')[0]
    j = judge(data)
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audit/validate",
                          json={'build_gold_ids':refs[:1],'audit_gold_ids':refs[1:]})
    assert response.status_code == 200, response.text
    snap = response.json()['snapshot']
    assert snap['build'][0]['source_group'] != snap['audit'][0]['source_group']
    assert snap['policy'] == POLICY
    assert db.one('SELECT COUNT(*) FROM ledger')[0] == calls
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_gold.confirmed'")[0] == 2
    assert JudgeService(db).get(j['id'])['status'] == 'draft'


@pytest.mark.parametrize('change', ['output','reference','rubric','audit'])
def test_changed_gold_context_blocks_preflight_before_model_calls(client, change):
    data, run, outputs = setup_gold(client)
    refs = [submit(client,data,context(client,data,oid)).json()['id'] for oid in outputs]
    j = judge(data)
    db = get_db()
    if change == 'output': db.execute("UPDATE outputs SET text='改过的输出' WHERE id=?", (outputs[1],))
    if change == 'reference': db.execute("UPDATE dataset_items SET evaluation_only_json='{}' WHERE id=?", (data['item_ids'][1],))
    if change == 'rubric':
        schema = json.loads(db.one('SELECT schema_json FROM rubrics WHERE id=?', (data['rubric_id'],))[0])
        schema['changed'] = True
        db.execute('UPDATE rubrics SET schema_json=? WHERE id=?', (json.dumps(schema),data['rubric_id']))
    if change == 'audit': db.execute("UPDATE audit_log SET payload_hash='wrong' WHERE action='severity_gold.confirmed'")
    calls = db.one('SELECT COUNT(*) FROM ledger')[0]
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audit/validate",
                          json={'build_gold_ids':refs[:1],'audit_gold_ids':refs[1:]})
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'GOLD_EVIDENCE_CHANGED'
    assert db.one('SELECT COUNT(*) FROM ledger')[0] == calls


@pytest.mark.parametrize('overrides', [{'confirmed_human_review':'true'}, {'labels':{'severity_1':0}},
    {'case_hash':'wrong'}, {'reviewer':''}, {'labels':{'severity_1':True,'severity_2':False},'evidence':[]}])
def test_gold_requires_explicit_complete_human_labels_and_fingerprints(client, overrides):
    data,_,outputs = setup_gold(client)
    assert submit(client,data,context(client,data,outputs[0]), **overrides).status_code in (409,422)


def test_cross_project_and_source_overlap_are_rejected(client):
    data,_,outputs = setup_gold(client)
    other = make_project(client)['id']
    response = client.get(f'/workflow-api/v1/projects/{other}/severity-gold/context',
                          params={'output_id':outputs[0],'rubric_id':data['rubric_id']})
    assert response.status_code == 422
    ref = submit(client,data,context(client,data,outputs[0])).json()['id']
    j = judge(data)
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audit/validate",
                          json={'build_gold_ids':[ref],'audit_gold_ids':[ref]})
    assert response.status_code == 422
    assert response.json()['code'] == 'SOURCE_OVERLAP'


def test_explicit_positive_gold_requires_a_real_quote_per_rule(client):
    data,_,outputs = setup_gold(client)
    ctx = context(client,data,outputs[0])
    labels = {r:r=='severity_1' for r in ctx['rules']}
    response = submit(client,data,ctx,labels=labels,
                      evidence=[{'rule_id':'severity_1','start':0,'end':2,'quote':ctx['text'][:2]}])
    assert response.status_code == 201, response.text


def test_severity_gold_arbitration_appends_a_new_leaf_and_supersedes_old_evidence(client):
    data,_,outputs = setup_gold(client)
    original_context = context(client,data,outputs[0])
    original = submit(client,data,original_context).json()['id']
    other = submit(client,data,context(client,data,outputs[1])).json()['id']
    load = client.get(f"/workflow-api/v1/projects/{data['pid']}/severity-gold/{original}/adjudication-context")
    assert load.status_code == 200, load.text
    arb_context = load.json()
    assert arb_context['current_gold']['labels'] == {r:False for r in arb_context['rules']}
    quote = arb_context['text'][:2]
    labels = {r:r=='severity_1' for r in arb_context['rules']}
    body = payload(arb_context, labels=labels, evidence=[{
        'rule_id':'severity_1','start':0,'end':2,'quote':quote}],
        reviewer='独立仲裁人',reason='复核后确认该规则被违反')
    response = client.post(f"/workflow-api/v1/projects/{data['pid']}/severity-gold/{original}/adjudicate",json=body)
    assert response.status_code == 201, response.text
    adjudicated = response.json()['id']
    db=get_db()
    rows=client.get(f"/workflow-api/v1/projects/{data['pid']}/annotations?gold_only=true").json()['annotations']
    by_id={r['id']:r for r in rows}
    assert by_id[original]['superseded'] is True
    assert by_id[adjudicated]['superseded'] is False
    assert by_id[adjudicated]['supersedes_id']==original
    assert by_id[adjudicated]['gold_status']=='adjudicated'
    assert json.loads(db.one('SELECT scores_json FROM annotations WHERE id=?',(original,))[0])['severity_gold']['labels']=={r:False for r in arb_context['rules']}
    assert client.post(f"/workflow-api/v1/projects/{data['pid']}/severity-gold/{original}/adjudicate",json=body).status_code==409
    judge_row=judge(data)
    old_preflight=client.post(f"/workflow-api/v1/judges/{judge_row['id']}/severity-audit/validate",
        json={'build_gold_ids':[original],'audit_gold_ids':[other]})
    assert old_preflight.status_code==409
    assert old_preflight.json()['code']=='GOLD_SUPERSEDED'
    new_preflight=client.post(f"/workflow-api/v1/judges/{judge_row['id']}/severity-audit/validate",
        json={'build_gold_ids':[adjudicated],'audit_gold_ids':[other]})
    assert new_preflight.status_code==200, new_preflight.text
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE action='severity_gold.adjudicated'")[0]==1


def test_consumed_exam_gold_uses_original_frozen_task_instead_of_scrubbed_table(client):
    data, run, _ = setup_gold(client)
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={'candidate_id':'baseline'}).status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 202
    row = get_db().one("SELECT o.id,i.runtime_input_json FROM outputs o JOIN dataset_items i ON i.id=o.item_id WHERE o.run_id=? AND i.split='sealed_test'", (run['id'],))
    assert row['runtime_input_json'] == '{}'
    ctx = context(client,data,row['id'])
    assert ctx['task']['input']['question']
    assert ctx['task']['reference']['expert_answer']
    assert submit(client,data,ctx).status_code == 201


def test_severity_gold_does_not_substitute_for_ordinal_score_gold(client):
    data,_,outputs = setup_gold(client)
    for oid in outputs:
        assert submit(client,data,context(client,data,oid)).status_code == 201
    j = judge(data)
    calls = get_db().one('SELECT COUNT(*) FROM ledger')[0]
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/calibrate", json={'build_refs':outputs[:1],'audit_refs':outputs[1:]})
    assert response.status_code == 422
    assert response.json()['code'] == 'GOLD_NOT_VERIFIED'
    assert get_db().one('SELECT COUNT(*) FROM ledger')[0] == calls


def test_audit_missing_frozen_severity_policy_is_rejected(client):
    data,_,outputs = setup_gold(client)
    refs = [submit(client,data,context(client,data,oid)).json()['id'] for oid in outputs]
    j = JudgeService(get_db()).create(data['pid'],data['rubric_id'],{'connection_id':'conn_mock'})
    response = client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audit/validate", json={'build_gold_ids':refs[:1],'audit_gold_ids':refs[1:]})
    assert response.status_code == 422
    assert response.json()['code'] == 'SEVERITY_POLICY_INVALID'

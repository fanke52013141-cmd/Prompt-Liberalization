import json
import pytest
from types import SimpleNamespace

from .conftest import setup_project_with_data, start_run
from prompt_lib.db import get_db
from prompt_lib.domain import JudgeService
from prompt_lib.judge_binding import admitted_for, evaluator_binding


@pytest.mark.parametrize('policy',[
    None,{}, {'min_groups':True,'min_exact_rate':.8,'max_abstain_rate':.1},
    {'min_groups':5,'min_exact_rate':1.1,'max_abstain_rate':.1},
    {'min_groups':5,'min_exact_rate':.8,'max_abstain_rate':False}])
def test_invalid_score_policy_is_rejected_before_judge_creation(client,policy):
    from prompt_lib.core import BizError
    data=setup_project_with_data(client)
    with pytest.raises(BizError) as error:
        JudgeService(get_db()).create(data['pid'],data['rubric_id'],
            {'connection_id':'conn_mock','calibration_policy':policy})
    assert error.value.code=='CALIBRATION_POLICY_INVALID'
    assert get_db().one('SELECT COUNT(*) FROM judges')[0]==0


@pytest.mark.parametrize('override',[
    {'min_positive_groups':True},{'min_negative_groups':0},
    {'min_recall_lower':False},{'max_false_positive_upper':1.01},
    {'max_unknown_rate':-0.01},{'confidence':.5},{'confidence':1},
    {'simultaneous':1}])
def test_invalid_severity_policy_api_creates_nothing_and_dispatches_nothing(client,override):
    from .test_severity_gold import POLICY
    data=setup_project_with_data(client)
    db=get_db()
    before=db.one('SELECT COUNT(*) FROM ledger')[0]
    response=client.post(f"/workflow-api/v1/projects/{data['pid']}/judges",json={
        'rubric_id':data['rubric_id'],'model_cfg':{'connection_id':'conn_mock',
            'severity_calibration_policy':{**POLICY,**override}}})
    assert response.status_code==422,response.text
    assert response.json()['code']=='SEVERITY_POLICY_INVALID'
    assert db.one('SELECT COUNT(*) FROM judges')[0]==0
    assert db.one('SELECT COUNT(*) FROM ledger')[0]==before


@pytest.mark.parametrize('policy',[None,{}])
def test_missing_explicit_severity_policy_is_rejected(client,policy):
    data=setup_project_with_data(client)
    response=client.post(f"/workflow-api/v1/projects/{data['pid']}/judges",json={
        'rubric_id':data['rubric_id'],'model_cfg':{'connection_id':'conn_mock',
                                              'severity_calibration_policy':policy}})
    assert response.status_code==422,response.text
    assert get_db().one('SELECT COUNT(*) FROM judges')[0]==0


def calibrated_fixture(data, config):
    db = get_db()
    judge = JudgeService(db).create(data['pid'], data['rubric_id'], config)
    metrics = {'admission': {'eligible': True, 'severe_admission': False},
               'evaluator_binding': evaluator_binding(db, data['rubric_id'], config)}
    db.execute("UPDATE judges SET status='audited',metrics_json=? WHERE id=?",
               (json.dumps(metrics), judge['id']))
    return db.one('SELECT * FROM judges WHERE id=?', (judge['id'],))


def test_admission_cannot_cross_models_parameters_or_rubric_content(client):
    data = setup_project_with_data(client)
    db = get_db()
    config = {'connection_id': 'conn_mock', 'model': 'mock-gen-1', 'params': {'temperature': 0}}
    judge = calibrated_fixture(data, config)
    assert admitted_for(db, judge, data['rubric_id'], config)
    assert not admitted_for(db, judge, data['rubric_id'], {**config, 'model': 'other-model'})
    assert not admitted_for(db, judge, data['rubric_id'], {**config, 'params': {'temperature': 1}})
    assert not admitted_for(db, judge, data['rubric_id'], {**config, 'params': {'temperature': 0, 'max_tokens': 100}})
    schema = json.loads(db.one('SELECT schema_json FROM rubrics WHERE id=?', (data['rubric_id'],))[0])
    schema['test_changed_anchor'] = True
    db.execute('UPDATE rubrics SET schema_json=? WHERE id=?', (json.dumps(schema), data['rubric_id']))
    assert not admitted_for(db, judge, data['rubric_id'], config)


def test_legacy_calibration_without_binding_is_not_admitted(client):
    data = setup_project_with_data(client)
    config = {'connection_id': 'conn_mock'}
    judge = calibrated_fixture(data, config)
    get_db().execute('UPDATE judges SET metrics_json=? WHERE id=?',
                     (json.dumps({'admission': {'eligible': True}}), judge['id']))
    judge = get_db().one('SELECT * FROM judges WHERE id=?', (judge['id'],))
    assert not admitted_for(get_db(), judge, data['rubric_id'], config)


def test_severe_flag_without_independent_audit_cannot_grant_admission(client):
    from prompt_lib.judge_binding import severe_admitted_for
    data=setup_project_with_data(client)
    config={'connection_id':'conn_mock'}
    judge=calibrated_fixture(data,config)
    metrics=json.loads(judge['metrics_json'])
    metrics['admission']['severe_admission']=True
    get_db().execute('UPDATE judges SET metrics_json=? WHERE id=?',(json.dumps(metrics),judge['id']))
    judge=get_db().one('SELECT * FROM judges WHERE id=?',(judge['id'],))
    assert admitted_for(get_db(),judge,data['rubric_id'],config)
    assert not severe_admitted_for(get_db(),judge,data['rubric_id'],config)


def test_evaluation_parameters_are_used_in_actual_request(client, monkeypatch):
    from prompt_lib import engine
    data = setup_project_with_data(client)
    captured = {}
    factory = engine._get_provider
    def provider(connection):
        inner = factory(connection)
        def complete(role, model, messages, params, fingerprint):
            captured.update(params)
            return inner.complete(role, model, messages, params, fingerprint)
        return SimpleNamespace(complete=complete)
    monkeypatch.setattr(engine, '_get_provider', provider)
    engine.evaluate_once('答案', data['rubric_id'],
                         {'connection_id': 'conn_mock', 'params': {'temperature': 0, 'max_tokens': 100}})
    assert captured == {'temperature': 0, 'max_tokens': 100}


def test_ordinal_calibration_does_not_admit_severe_detector(client):
    from prompt_lib.core import canonical_hash
    data = setup_project_with_data(client)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=0)
    assert run['state'] == 'completed'
    snapshot = run['snapshot']
    judge = calibrated_fixture(data, snapshot['models']['evaluation'])
    snapshot['judge_id'] = judge['id']
    # Isolated fixture: production snapshots cannot be amended through the API.
    get_db().execute('UPDATE runs SET snapshot_json=?,snapshot_hash=? WHERE id=?',
                     (json.dumps(snapshot), canonical_hash(snapshot), run['id']))
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={'candidate_id': 'baseline'}).status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 202, response.text
    report = response.json()
    assert report['gates']['评价器准入']['result'] == '通过'
    assert report['gates']['严重错误门槛']['result'] == '需人工复核'
    assert '独立人工金标' in report['gates']['严重错误门槛']['detail']
    assert report['stats']['severe_upper_bound_95'] is None
    assert report['stats']['evaluator_evidence']['severe_admitted'] is False
    assert not report['eligibility'].startswith('可正式采用')


def test_connection_identity_changes_but_credential_rotation_preserves_calibration(client):
    data = setup_project_with_data(client)
    db = get_db()
    config = {'connection_id': 'conn_mock'}
    judge = calibrated_fixture(data, config)
    connections = json.loads(db.one("SELECT value_json FROM settings WHERE key='connections'")[0])
    connections[0]['api_key'] = 'isolated-placeholder-new-key'
    db.execute("UPDATE settings SET value_json=? WHERE key='connections'", (json.dumps(connections),))
    assert admitted_for(db, judge, data['rubric_id'], config)
    connections[0]['model'] = 'different-model'
    db.execute("UPDATE settings SET value_json=? WHERE key='connections'", (json.dumps(connections),))
    assert not admitted_for(db, judge, data['rubric_id'], config)


def test_actual_calibration_persists_execution_binding_without_claiming_low_support_valid(client,monkeypatch):
    from prompt_lib.domain import AnnotationService
    data = setup_project_with_data(client)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=0)
    db = get_db()
    outputs = db.query('SELECT id FROM outputs WHERE run_id=? ORDER BY created_at', (run['id'],))
    assert len(outputs) == 2
    schema = json.loads(db.one('SELECT schema_json FROM rubrics WHERE id=?', (data['rubric_id'],))[0])
    scores = {dimension['name']: 3 for dimension in schema['dimensions']}
    blind = AnnotationService(db)
    for output in outputs:
        annotation = blind.model_pre_annotation(data['pid'], output['id'], data['rubric_id'], scores)
        blind.verify_annotation(annotation['id'])
    config = {'connection_id': 'conn_mock', 'params': {'max_tokens': 1000},
              'calibration_policy': {'min_groups': 5, 'min_exact_rate': 0.8, 'max_abstain_rate': 0.1}}
    service = JudgeService(db)
    judge = service.create(data['pid'], data['rubric_id'], config)
    historical={'audit_id':'historical-reference','snapshot_hash':'old-snapshot'}
    db.execute('UPDATE judges SET metrics_json=? WHERE id=?',
        (json.dumps({'severity_audit':historical,'admission':{'severe_admission':True}}),judge['id']))
    audited = service.calibrate(judge['id'], [outputs[0]['id']], [outputs[1]['id']])
    assert audited['status'] == 'audited'
    assert audited['metrics']['evaluator_binding'] == evaluator_binding(db, data['rubric_id'], config)
    assert not audited['metrics']['admission']['eligible']
    assert not audited['metrics']['admission']['severe_admission']
    assert audited['metrics']['severity_audit']==historical
    assert audited['metrics']['severity_reactivation_required'] is True
    before=db.one('SELECT metrics_json FROM judges WHERE id=?',(judge['id'],))[0]
    original=service._score_set
    def change_current_config(*args):
        result=original(*args)
        db.execute('UPDATE judges SET model_config_json=? WHERE id=?',
            (json.dumps({**config,'model':'changed-mid-calibration'}),judge['id']))
        return result
    monkeypatch.setattr(service,'_score_set',change_current_config)
    import pytest
    from prompt_lib.core import BizError
    with pytest.raises(BizError) as error:
        service.calibrate(judge['id'],[outputs[0]['id']],[outputs[1]['id']])
    assert error.value.code=='CALIBRATION_CONTEXT_CHANGED'
    assert db.one('SELECT metrics_json FROM judges WHERE id=?',(judge['id'],))[0]==before

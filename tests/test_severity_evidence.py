import json

import pytest

from prompt_core.evaluation import severity_rules, validate_rubric_result


def evaluate(severe, violations):
    return validate_rubric_result({'scores': {'quality': 3}, 'abstain': False,
                                  'severe': severe, 'violations': violations}, ['quality'],
                                 output_text='😀错误答案', rules={'r1':'不得给出错误答案'})


def test_locatable_severity_evidence_preserves_score_and_rule():
    evidence = {'rule_id':'r1', 'start':1, 'end':3, 'quote':'错误'}
    result = evaluate(True, [evidence])
    assert result['severe'] is True
    assert result['scores'] == {'quality': 3}
    assert result['violations'] == [evidence]
    assert not result['abstain']


@pytest.mark.parametrize('severe,violations', [(True, []), (False, [{'rule_id':'r1','start':1,'end':3,'quote':'错误'}]),
    (True, None), (True, 'quote'), (True, [{}]),
    (True, [{'rule_id':'invented','start':1,'end':3,'quote':'错误'}]),
    (True, [{'rule_id':'r1','start':True,'end':3,'quote':'错误'}]),
    (True, [{'rule_id':'r1','start':1,'end':3,'quote':'正确'}]),
    (True, [{'rule_id':'r1','start':2,'end':4,'quote':'错误'}]),
    (True, [{'rule_id':'r1','start':1,'end':1,'quote':''}])])
def test_missing_fabricated_or_contradictory_severity_is_unknown_without_erasing_score(severe, violations):
    result = evaluate(severe, violations)
    assert result['severe'] is None
    assert result['severity_error']
    assert result['scores'] == {'quality':3}
    assert result['abstain'] is False
    assert result['violations'] == []


def test_no_rule_context_cannot_become_safe():
    result = validate_rubric_result({'scores':{'quality':3},'abstain':False,'severe':False},
                                   ['quality'], output_text='答案', rules={})
    assert result['severe'] is None
    assert evaluate(False, [])['severe'] is False


@pytest.mark.parametrize('entries', ['text', [None], [''],
    [{'id':'r1','description':''}], ['rule', {'id':'severity_1','description':'duplicate'}]])
def test_invalid_rule_schemas_are_rejected(entries):
    with pytest.raises(ValueError):
        severity_rules({'severity_examples':entries})


def test_engine_validates_model_evidence_against_actual_text_and_frozen_rule_ids(client, monkeypatch):
    from .conftest import setup_project_with_data
    from prompt_lib import engine
    from prompt_lib.providers import CallResult
    data = setup_project_with_data(client)
    captured = []
    violation = {'rule_id':'severity_1', 'start':1, 'end':3, 'quote':'错误'}
    def respond(*args):
        captured.append(args[3])
        from prompt_lib.db import get_db
        schema = json.loads(get_db().one('SELECT schema_json FROM rubrics WHERE id=?', (data['rubric_id'],))[0])
        scores = {d['name']:3 for d in schema['dimensions']}
        return CallResult(json.dumps({'scores':scores,'abstain':False,'severe':True,'violations':[violation]}), 'stop', {'in':1,'out':1})
    monkeypatch.setattr(engine, 'call_model', respond)
    result = engine.evaluate_once('😀错误答案', data['rubric_id'], {'connection_id':'conn_mock'})
    assert result['severe'] is True
    assert 'severity_1' in captured[0][0]['content']
    violation['quote'] = '编造引用'
    result = engine.evaluate_once('😀错误答案', data['rubric_id'], {'connection_id':'conn_mock'})
    assert result['severe'] is None and not result['abstain']

import json

import pytest

from .conftest import setup_project_with_data, start_run, wait_run
from prompt_lib.db import get_db


def setup_exam(client, n=12, max_candidates=0):
    data = setup_project_with_data(client, n=n)
    template = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                         dev_ids=data['item_ids'][:2], max_candidates=0)
    draft = template['snapshot']
    draft['acceptance'] = {'evaluation_source': 'human'}
    draft['optimization']['max_candidates'] = max_candidates
    response = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs", json=draft)
    assert response.status_code == 202, response.text
    run = wait_run(client, response.json()['id'])
    assert run['state'] == 'completed'
    target = run['candidates'][0]['candidate_id'] if max_candidates else 'baseline'
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={'candidate_id': target}).status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 202, response.text
    assert response.json()['state'] == 'waiting_human'
    return data, run, client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-review").json()


def rating(view, pair, side, **overrides):
    return {'context_hash': view['context_hash'], 'output_hash': pair['sides'][side]['output_hash'],
            'usable': True, 'severe': False, 'reviewer': '离线测试评审人', 'reason': '按冻结合同检查输出',
            'categories': [], 'evidence': [], **overrides}


def submit(client, run, view, pair, side, **overrides):
    return client.post(f"/workflow-api/v1/runs/{run['id']}/acceptance-review/{pair['public_id']}/{side}",
                       json=rating(view, pair, side, **overrides))


def test_complete_human_exam_is_durable_blind_and_never_calls_model_evaluation(client):
    from prompt_lib.db import DB, set_db
    data, run, view = setup_exam(client)
    db = get_db()
    ledger_count = db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (run['id'],))[0]
    assert ledger_count == 4
    assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance' AND role='evaluation'", (run['id'],))[0] == 0
    assert not view['complete'] and len(view['pairs']) == 2
    blob = json.dumps(view)
    for field in ('prompt_version_id', 'output_id', 'candidate', 'baseline', 'item_id', 'run_id'):
        assert field not in blob
    output_id = next(iter(json.loads(db.one('SELECT review_json FROM acceptance_jobs WHERE run_id=?', (run['id'],))[0]).values()))['outputs'][0]['output_id']
    assert client.get(f'/workflow-api/v1/outputs/{output_id}').status_code == 409
    assert output_id not in client.get(f"/workflow-api/v1/projects/{data['pid']}/outputs").text
    pair = view['pairs'][0]
    assert submit(client, run, view, pair, 'A').status_code == 200
    assert submit(client, run, view, pair, 'A').json()['idempotent'] is True
    assert submit(client, run, view, pair, 'A', usable=False).status_code == 409
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()['state'] == 'waiting_human'
    path = db.path
    db._conn.close()
    set_db(DB(str(path)))
    db = get_db()
    recovered = client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-review").json()
    assert recovered['context_hash'] == view['context_hash']
    assert recovered['pairs'][0]['sides']['A']['submitted']
    for pair in recovered['pairs']:
        for side in ('A', 'B'):
            if not pair['sides'][side]['submitted']:
                assert submit(client, run, recovered, pair, side).status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 202, response.text
    report = response.json()
    assert report['gates']['评价器准入']['result'] == '通过'
    assert report['gates']['严重错误门槛']['result'] == '通过'
    assert report['stats']['evaluator_evidence']['source'] == 'human'
    assert report['stats']['evaluator_evidence']['human_evidence']['side_count'] == 4
    assert report['stats']['evaluator_evidence']['human_evidence']['review_duration']['seconds'] is None
    usage = report['stats']['acceptance_usage']
    assert usage['phase'] == 'acceptance'
    assert usage['physical_attempts'] == 4
    assert usage['settled_attempts'] == 4
    assert usage['pending_attempts'] == 0
    assert usage['by_role']['generation']['attempts'] == 4
    assert usage['by_role']['generation']['known_tokens'] == usage['known_tokens']
    assert '不是供应商账单' in usage['cost_basis']
    assert report['decision'] == 'no_improvement'
    assert client.get(f'/workflow-api/v1/outputs/{output_id}').status_code == 200
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()['id'] == report['id']
    assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (run['id'],))[0] == ledger_count


@pytest.mark.parametrize('overrides', [ {'usable': 1}, {'severe': 'false'}, {'reviewer': ''},
    {'output_hash': 'wrong'}, {'context_hash': 'wrong'}, {'severe': True},
    {'severe': True, 'evidence': [{'start': 0, 'end': 1, 'quote': 'not actual', 'rule_id': 'rule'}]},
    {'review_elapsed_seconds': -1}, {'review_elapsed_seconds': 21601},
    {'review_elapsed_seconds': 1.5}, {'review_elapsed_seconds': True} ])
def test_human_invalid_or_unbound_evidence_is_rejected(client, overrides):
    _, run, view = setup_exam(client)
    response = submit(client, run, view, view['pairs'][0], 'A', **overrides)
    assert response.status_code in (409, 422), response.text
    updated = client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-review").json()
    assert not updated['pairs'][0]['sides']['A']['submitted']


def test_browser_foreground_duration_is_bounded_audited_and_aggregated(client):
    from prompt_lib.human_acceptance import HumanAcceptance
    _, run, view = setup_exam(client)
    pair = view['pairs'][0]
    assert submit(client, run, view, pair, 'A', review_elapsed_seconds=11).status_code == 200
    # A lost-response retry can advance telemetry without changing the locked decision.
    retry = submit(client, run, view, pair, 'A', review_elapsed_seconds=18)
    assert retry.status_code == 200 and retry.json()['idempotent'] is True
    for index, pair in enumerate(view['pairs']):
        for side in ('A', 'B'):
            if pair['public_id'] == view['pairs'][0]['public_id'] and side == 'A':
                continue
            assert submit(client, run, view, pair, side,
                          review_elapsed_seconds=20 + index * 2 + (side == 'B')).status_code == 200
    report = client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()
    evidence = HumanAcceptance(get_db()).evidence(run['id'])
    duration = evidence['review_duration']
    assert duration['measurement'] == 'browser_foreground_page_seconds'
    assert duration['seconds'] == 23
    assert duration['timed_side_count'] == duration['side_count'] == 4
    assert '不代表可核验的净工时' in duration['note']
    assert report['stats']['evaluator_evidence']['human_evidence'] == evidence


def test_human_unknown_risk_does_not_become_zero_risk(client):
    _, run, view = setup_exam(client)
    for pair in view['pairs']:
        for side in ('A', 'B'):
            assert submit(client, run, view, pair, side, severe=None).status_code == 200
    report = client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()
    assert report['gates']['严重错误门槛']['result'] != '通过'
    assert report['stats']['severe_upper_bound_95'] is None
    assert not report['eligibility'].startswith('可正式采用')


def test_human_rating_audit_tampering_blocks_report(client):
    _, run, view = setup_exam(client)
    for pair in view['pairs']:
        for side in ('A', 'B'):
            assert submit(client, run, view, pair, side).status_code == 200
    db = get_db()
    db.execute("UPDATE audit_log SET payload_hash='changed' WHERE action='acceptance.human_rating'")
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 409, response.text
    assert db.one('SELECT COUNT(*) FROM acceptance_reports WHERE run_id=?', (run['id'],))[0] == 0


def test_identical_outputs_cannot_gain_from_contradictory_human_labels(client):
    _, run, view = setup_exam(client)
    pair = view['pairs'][0]
    assert pair['sides']['A']['output_hash'] == pair['sides']['B']['output_hash']
    assert submit(client, run, view, pair, 'A').status_code == 200
    response = submit(client, run, view, pair, 'B', usable=False)
    assert response.status_code == 409
    assert response.json()['code'] == 'HUMAN_IDENTICAL_CONFLICT'


def test_human_severe_evidence_with_unicode_is_kept_and_blocks_safety(client):
    _, run, view = setup_exam(client)
    for pair in view['pairs']:
        for side in ('A', 'B'):
            quote = pair['sides'][side]['text'][:2]
            assert submit(client, run, view, pair, side, severe=True,
                          evidence=[{'rule_id':'severity_1', 'start':0, 'end':2, 'quote':quote}]).status_code == 200
    report = client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()
    assert report['gates']['严重错误门槛']['result'] == '未通过'
    assert report['stats']['severe_candidate'] == 2
    assert report['decision'] == 'regression'


def test_positive_human_report_can_publish_only_with_original_review_evidence(client):
    from prompt_lib.human_acceptance import HumanAcceptance
    data, run, view = setup_exam(client, n=20, max_candidates=1)
    db = get_db()
    store = json.loads(db.one('SELECT review_json FROM acceptance_jobs WHERE run_id=?', (run['id'],))[0])
    for pair in view['pairs']:
        entry = next(r for r in store.values() if r['public_id'] == pair['public_id'])
        assert pair['sides']['A']['output_hash'] != pair['sides']['B']['output_hash']
        for index, side in enumerate(('A', 'B')):
            # Synthetic human decisions exercise publication, not real task benefit.
            assert submit(client, run, view, pair, side,
                          usable=entry['outputs'][index]['side'] == 'candidate').status_code == 200
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/accept")
    assert response.status_code == 202, response.text
    report = response.json()
    assert report['decision'] == 'verified_improvement', report
    assert report['eligibility'].startswith('可正式采用')
    body = {'prompt_version_id':report['candidate_ref'], 'report_ref':report['id'], 'mode':'active'}
    publish = client.post(f"/workflow-api/v1/projects/{data['pid']}/releases", json=body)
    assert publish.status_code == 201, publish.text
    assert HumanAcceptance(db).evidence(run['id']) == report['stats']['evaluator_evidence']['human_evidence']
    db.execute("UPDATE audit_log SET payload_hash='tampered' WHERE action='acceptance.human_rating'")
    publish = client.post(f"/workflow-api/v1/projects/{data['pid']}/releases", json=body)
    assert publish.status_code == 409, publish.text
    assert publish.json()['code'] == 'HUMAN_EVIDENCE_CHANGED'


def test_human_cannot_invent_a_business_severity_rule(client):
    _, run, view = setup_exam(client)
    pair = view['pairs'][0]
    quote = pair['sides']['A']['text'][:1]
    response = submit(client, run, view, pair, 'A', severe=True,
                      evidence=[{'rule_id':'unlisted_rule','start':0,'end':1,'quote':quote}])
    assert response.status_code == 422
    assert response.json()['code'] == 'HUMAN_RULE_INVALID'


def test_truncated_output_is_unusable_but_can_still_contain_a_proven_severe_error(client, monkeypatch):
    from types import SimpleNamespace
    from prompt_lib import engine
    from prompt_lib.providers import CallResult
    factory = engine._get_provider
    def provider(config):
        inner = factory(config)
        def complete(role, model, messages, params, fingerprint):
            result = inner.complete(role, model, messages, params, fingerprint)
            return CallResult(result.text, 'length', result.usage) if role == 'generation' else result
        return SimpleNamespace(complete=complete)
    monkeypatch.setattr(engine, '_get_provider', provider)
    _, run, view = setup_exam(client)
    for pair in view['pairs']:
        for side in ('A','B'):
            assert pair['sides'][side]['generation_status'] == 'incomplete'
            quote = pair['sides'][side]['text'][:1]
            ev = [{'rule_id':'severity_1','start':0,'end':1,'quote':quote}]
            assert submit(client, run, view, pair, side, usable=True, severe=True, evidence=ev).status_code == 422
            assert submit(client, run, view, pair, side, usable=False, severe=True, evidence=ev).status_code == 200
    report = client.post(f"/workflow-api/v1/runs/{run['id']}/accept").json()
    assert report['stats']['candidate_usable_rate'] == 0
    assert report['stats']['severe_candidate'] == 2
    assert report['gates']['严重错误门槛']['result'] == '未通过'

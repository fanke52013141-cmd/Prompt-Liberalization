from types import SimpleNamespace

import pytest

from .conftest import setup_project_with_data, start_run, wait_run
from prompt_lib import engine
from prompt_lib.db import get_db
from prompt_lib.providers import ProviderError


@pytest.mark.parametrize('failed_role', ['generation', 'evaluation', 'optimizer'])
@pytest.mark.parametrize('resolution', ['response', 'not_accepted', 'retry'])
def test_complete_run_pauses_for_reconciliation_and_resumes_same_observations(client, monkeypatch, failed_role, resolution):
    data = setup_project_with_data(client)
    factory = engine._get_provider
    captured = {}

    def provider(config):
        underlying = factory(config)
        def complete(role, model, messages, params, fingerprint):
            result = underlying.complete(role, model, messages, params, fingerprint)
            if role == failed_role and not captured:
                captured['response'] = result
                raise ProviderError('NETWORK', 'offline response lost', retryable=False)
            return result
        return SimpleNamespace(complete=complete)

    monkeypatch.setattr(engine, '_get_provider', provider)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=1)
    assert run['state'] == 'paused_interrupted', run
    assert run['stop_reason'] == 'call_result_unconfirmed'
    db = get_db()
    count = db.one('SELECT COUNT(*) FROM ledger WHERE run_id=?', (run['id'],))[0]
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/resume").status_code == 200
    still_paused = wait_run(client, run['id'])
    assert still_paused['state'] == 'paused_interrupted'
    assert db.one('SELECT COUNT(*) FROM ledger WHERE run_id=?', (run['id'],))[0] == count
    old = db.one("SELECT * FROM ledger WHERE run_id=? AND status='sent_unknown'", (run['id'],))
    assert old['role'] == failed_role
    body = {'request_hash': old['request_hash'], 'reason': '核对后继续原运行', 'evidence': '隔离Mock供应商记录已核对'}
    prefix = f"/workflow-api/v1/runs/{run['id']}/ledger/{old['attempt_id']}"
    if resolution == 'response':
        result = captured['response']
        body.update(text=result.text, finish=result.finish, actual_in=result.usage['in'], actual_out=result.usage['out'])
        response = client.post(prefix + '/reconcile-response', json=body)
    elif resolution == 'not_accepted':
        response = client.post(prefix + '/confirm-not-accepted', json={**body, 'confirmed_not_accepted': True})
    else:
        response = client.post(prefix + '/authorize-retry', json={**body, 'confirmed_possible_duplicate': True})
    assert response.status_code == 200, response.text
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/resume").status_code == 200
    completed = wait_run(client, run['id'])
    assert completed['state'] == 'completed', completed
    assert completed['snapshot_hash'] == run['snapshot_hash']
    details = client.get(f"/workflow-api/v1/runs/{run['id']}?detail=true").json()['baseline_detail']
    assert details['evaluation_coverage'] == 1
    assert len(details['items']) == 2
    assert len({item['item_id'] for item in details['items']}) == 2
    assert len(completed['rounds']) == 1
    assert db.one('SELECT COUNT(*) FROM ledger WHERE run_id=?', (run['id'],))[0] == (9 if resolution == 'response' else 10)
    preserved = db.one('SELECT * FROM ledger WHERE attempt_id=?', (old['attempt_id'],))
    if resolution == 'retry':
        assert preserved['status'] == 'sent_unknown'
        assert preserved['reserved_tokens'] == old['reserved_tokens']
    assert not db.one("SELECT 1 FROM acceptance_reports WHERE run_id=?", (run['id'],))


@pytest.mark.parametrize('failed_role', ['generation', 'evaluation'])
@pytest.mark.parametrize('resolution', ['response', 'not_accepted', 'retry'])
def test_exam_pauses_without_report_and_resumes_same_bound_cases(client, monkeypatch, failed_role, resolution):
    data = setup_project_with_data(client)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=0)
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/lock", json={'candidate_id': 'baseline'}).status_code == 200
    factory = engine._get_provider
    captured = {}

    def provider(config):
        underlying = factory(config)
        def complete(role, model, messages, params, fingerprint):
            result = underlying.complete(role, model, messages, params, fingerprint)
            if role == failed_role and not captured:
                captured['response'] = result
                raise ProviderError('NETWORK', 'offline exam response lost', retryable=False)
            return result
        return SimpleNamespace(complete=complete)

    monkeypatch.setattr(engine, '_get_provider', provider)
    url = f"/workflow-api/v1/runs/{run['id']}/accept"
    assert client.post(url).status_code == 409
    db = get_db()
    job = client.get(f"/workflow-api/v1/runs/{run['id']}/acceptance-job").json()['job']
    assert job['state'] == 'paused_interrupted'
    assert not db.one('SELECT 1 FROM acceptance_reports WHERE run_id=?', (run['id'],))
    count = db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (run['id'],))[0]
    assert client.post(url).status_code == 409
    assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (run['id'],))[0] == count
    old = db.one("SELECT * FROM ledger WHERE run_id=? AND status='sent_unknown'", (run['id'],))
    body = {'request_hash': old['request_hash'], 'reason': '核对后继续考试', 'evidence': '隔离Mock供应商记录已核对'}
    prefix = f"/workflow-api/v1/runs/{run['id']}/ledger/{old['attempt_id']}"
    if resolution == 'response':
        result = captured['response']
        body.update(text=result.text, finish=result.finish, actual_in=result.usage['in'], actual_out=result.usage['out'])
        response = client.post(prefix + '/reconcile-response', json=body)
    elif resolution == 'not_accepted':
        response = client.post(prefix + '/confirm-not-accepted', json={**body, 'confirmed_not_accepted': True})
    else:
        response = client.post(prefix + '/authorize-retry', json={**body, 'confirmed_possible_duplicate': True})
    assert response.status_code == 200, response.text
    report = client.post(url)
    assert report.status_code == 202, report.text
    assert report.json()['acceptance_id'] == job['id']
    assert report.json()['stats']['group_n'] == 2
    assert client.post(url).json()['id'] == report.json()['id']
    assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (run['id'],))[0] == (8 if resolution == 'response' else 9)
    assert db.one('SELECT COUNT(*) FROM acceptance_reports WHERE run_id=?', (run['id'],))[0] == 1

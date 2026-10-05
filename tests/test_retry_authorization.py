import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from .conftest import setup_project_with_data, start_run
from prompt_lib.core import BizError
from prompt_lib.db import get_db
from prompt_lib.domain import DataService, ProjectsService, PromptService
from prompt_lib.engine import generate_once
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.providers import CallResult, ProviderError


@pytest.mark.parametrize('retry_fails', [False, True])
def test_authorized_retry_keeps_old_cost_and_grant_is_consumed_once(client, retry_fails):
    data = setup_project_with_data(client)
    run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                    dev_ids=data['item_ids'][:2], max_candidates=0)
    db = get_db()
    provider = SimpleNamespace(complete=Mock(side_effect=[
        ProviderError('NETWORK', 'original unknown', retryable=False),
        ProviderError('NETWORK', 'retry unknown', retryable=True) if retry_fails else
        CallResult('retry result', 'stop', {'in': 10, 'out': 5})]))
    args = (db, ProjectsService(db).get(data['pid']), PromptService(db).get(data['prompt_id']),
            DataService(db).get_item_runtime(data['pid'], data['item_ids'][2]),
            run['snapshot']['models']['generation'], run['id'], 'search', BudgetState(run['budget']), Ledger(db))
    logical_id = 'authorized-retry'
    with patch('prompt_lib.engine._get_provider', return_value=provider):
        first = generate_once(*args, logical_id=logical_id)
        old = db.one('SELECT * FROM ledger WHERE run_id=? AND logical_id=?', (run['id'], f"{run['id']}:{logical_id}"))
        url = f"/workflow-api/v1/runs/{run['id']}/ledger/{old['attempt_id']}/authorize-retry"
        body = {'request_hash': old['request_hash'], 'reason': '决定重试', 'evidence': '查询未能确认原结果', 'confirmed_possible_duplicate': True}
        assert client.post(url, json={**body, 'confirmed_possible_duplicate': False}).status_code == 422
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json=body).status_code == 200
        assert provider.complete.call_count == 1
        original_budget = db.one('SELECT budget_state_json FROM runs WHERE id=?', (run['id'],))[0]
        tiny = json.loads(original_budget)
        tiny['search_limit'] = tiny['spent']['search'] + tiny['reserved']['search']
        db.execute('UPDATE runs SET budget_state_json=? WHERE id=?', (json.dumps(tiny), run['id']))
        with pytest.raises(BizError) as error:
            generate_once(*args, logical_id=logical_id)
        assert error.value.code == 'BUDGET_EXHAUSTED'
        assert db.one('SELECT retry_consumed FROM ledger WHERE attempt_id=?', (old['attempt_id'],))[0] == 0
        assert provider.complete.call_count == 1
        db.execute('UPDATE runs SET budget_state_json=? WHERE id=?', (original_budget, run['id']))
        second = generate_once(*args, logical_id=logical_id)
        assert second['id'] != first['id']
        assert second['status'] == ('failed' if retry_fails else 'ok')
        assert provider.complete.call_count == 2  # A retryable error cannot trigger a second authorized request.
        assert client.post(url, json=body).status_code == 200
        assert generate_once(*args, logical_id=logical_id)['id'] == second['id']
        assert provider.complete.call_count == 2
    preserved = db.one('SELECT * FROM ledger WHERE attempt_id=?', (old['attempt_id'],))
    assert preserved['status'] == 'sent_unknown'
    assert preserved['reserved_tokens'] == old['reserved_tokens']
    assert preserved['retry_consumed'] == 1
    with pytest.raises(BizError) as stale_grant:
        Ledger(db).reserve(run['id'], f"{run['id']}:{logical_id}", 'generation', 'mock-gen-1', 'search',
                           10, BudgetState(run['budget']), old['request_hash'],
                           retry_authorization_id=old['attempt_id'])
    assert stale_grant.value.code == 'RETRY_AUTHORIZATION_CONSUMED'
    assert db.one("SELECT COUNT(*) FROM audit_log WHERE target=? AND action='ledger.authorize_retry'", (old['attempt_id'],))[0] == 1

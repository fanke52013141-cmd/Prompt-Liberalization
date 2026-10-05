from copy import deepcopy

import pytest

from prompt_core.severity_calibration import audit_severity


POLICY = {'min_positive_groups': 10, 'min_negative_groups': 10, 'min_recall_lower': .8,
          'max_false_positive_upper': .1, 'max_unknown_rate': .1, 'confidence': .95, 'simultaneous': False}


def rows(n=100):
    return [{'source_group':f'p{i}', 'human_gold':{'r':True}, 'prediction':{'r':True}} for i in range(n)] + [
        {'source_group':f'n{i}', 'human_gold':{'r':False}, 'prediction':{'r':False}} for i in range(n)]


def audit(data, policy=POLICY, build=None):
    return audit_severity(data, ['r'], policy, build_sources=build or ['construction'])


def test_perfect_audit_has_uncertainty_and_can_meet_frozen_policy():
    result = audit(rows())
    assert result['eligible']
    metric = result['per_rule']['r']
    assert 0 < metric['recall_interval']['lower'] < 1
    assert 0 < metric['false_positive_interval']['upper'] < 1
    assert result['policy'] == POLICY


def test_no_positive_or_negative_support_never_looks_reliable():
    for data in (rows()[100:], rows()[:100], []):
        result = audit(data)
        assert not result['eligible']
        assert any('SUPPORT_LOW' in reason for reason in result['reasons'])


def test_unknown_predictions_do_not_disappear_from_denominators():
    data = rows()
    for row in data:
        row['prediction']['r'] = None
    result = audit(data)
    metric = result['per_rule']['r']
    assert metric['positive_groups'] == metric['negative_groups'] == 100
    assert metric['true_positive'] == 0
    assert metric['false_positive_worst_case'] == 100
    assert not result['eligible']


@pytest.mark.parametrize('change,code', [('duplicate','DUPLICATE_SOURCE'), ('overlap','SOURCE_OVERLAP'),
    ('missing','SOURCE_MISSING'), ('boolean','LABELS_INVALID'), ('score','LABELS_INVALID')])
def test_invalid_audit_cannot_pass(change, code):
    data = rows()
    if change == 'duplicate': data.append(deepcopy(data[0]))
    if change == 'overlap': data[0]['source_group'] = 'construction'
    if change == 'missing': data[0]['source_group'] = ''
    if change == 'boolean': data[0]['human_gold']['r'] = 1
    if change == 'score': data[0]['human_gold'] = {'quality':0}
    assert audit(data)['reasons'] == [code]


def test_unknown_gold_is_not_negative_gold():
    data = rows()
    data[0]['human_gold']['r'] = None
    result = audit(data)
    assert 'GOLD_UNKNOWN:r' in result['reasons']
    assert result['per_rule']['r']['gold_unknown'] == 1


def test_family_correction_widens_intervals():
    ordinary = audit(rows())['per_rule']['r']
    corrected = audit(rows(), {**POLICY, 'simultaneous':True})['per_rule']['r']
    assert corrected['recall_interval']['lower'] < ordinary['recall_interval']['lower']
    assert corrected['false_positive_interval']['upper'] > ordinary['false_positive_interval']['upper']


@pytest.mark.parametrize('policy', [None, {}, {**POLICY,'confidence':1},
    {**POLICY,'min_positive_groups':True}, {**POLICY,'min_recall_lower':float('nan')}])
def test_missing_or_invalid_frozen_policy_blocks(policy):
    assert not audit(rows(), policy)['eligible']


def test_every_rule_must_pass_instead_of_hiding_failure_in_an_average():
    data = rows()
    for row in data:
        row['human_gold']['bad_rule'] = row['human_gold']['r']
        row['prediction']['bad_rule'] = not row['prediction']['r']
    result = audit_severity(data, ['r','bad_rule'], POLICY, build_sources=['construction'])
    assert not result['eligible']
    assert 'RECALL_LOWER_LOW:bad_rule' in result['reasons']
    assert 'FALSE_POSITIVE_UPPER_HIGH:bad_rule' in result['reasons']
    assert not any(reason.endswith(':r') for reason in result['reasons'])


def test_confidence_too_close_to_one_cannot_overflow_into_an_infinite_claim():
    result = audit(rows(), {**POLICY,'confidence':1 - 1e-16, 'simultaneous':True})
    assert result['reasons'] == ['POLICY_PRECISION_UNSUPPORTED']

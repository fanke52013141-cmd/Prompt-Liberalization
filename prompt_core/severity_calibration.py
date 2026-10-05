"""Independent, per-rule severity audit; ordinal scores are never severity gold."""
from math import isfinite, sqrt
from statistics import NormalDist


def wilson_one_sided(successes, total, confidence):
    if total == 0:
        return None
    z = NormalDist().inv_cdf(confidence)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {'lower': max(0.0, center - radius), 'upper': min(1.0, center + radius)}


def audit_severity(rows, rule_ids, policy, *, build_sources):
    """Rows carry explicit human gold and validated model booleans per rule.

    Unknown positive predictions count against recall; unknown negative predictions
    count toward worst-case false positives. Source groups cannot be duplicated or
    shared with the construction set. These calculations do not authenticate gold.
    """
    rejected = lambda reason: {'eligible': False, 'scope': 'per_rule_severity', 'reasons': [reason], 'per_rule': {}}
    if not isinstance(rule_ids, list) or not rule_ids or any(not isinstance(r, str) or not r.strip() for r in rule_ids) or len(set(rule_ids)) != len(rule_ids):
        return rejected('RULES_INVALID')
    required = ('min_positive_groups', 'min_negative_groups', 'min_recall_lower',
                'max_false_positive_upper', 'max_unknown_rate', 'confidence', 'simultaneous')
    if not isinstance(policy, dict) or any(k not in policy for k in required):
        return rejected('POLICY_NOT_FROZEN')
    if any(type(policy[k]) is not int or policy[k] < 1 for k in required[:2]):
        return rejected('POLICY_INVALID')
    if any(type(policy[k]) not in (int, float) or not isfinite(policy[k]) or not 0 <= policy[k] <= 1 for k in required[2:5]):
        return rejected('POLICY_INVALID')
    if type(policy['confidence']) not in (int, float) or not 0.5 < policy['confidence'] < 1 or type(policy['simultaneous']) is not bool:
        return rejected('POLICY_INVALID')
    if not isinstance(rows, list) or not isinstance(build_sources, list) or any(not isinstance(g, str) or not g.strip() for g in build_sources):
        return rejected('AUDIT_INVALID')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('source_group'), str) or not row['source_group'].strip():
            return rejected('SOURCE_MISSING')
        group = row['source_group']
        if group in seen:
            return rejected('DUPLICATE_SOURCE')
        if group in build_sources:
            return rejected('SOURCE_OVERLAP')
        seen.add(group)
        for field in ('human_gold', 'prediction'):
            values = row.get(field)
            if not isinstance(values, dict) or set(values) != set(rule_ids) or any(v is not None and type(v) is not bool for v in values.values()):
                return rejected('LABELS_INVALID')
    # Optional Bonferroni correction covers both tails for every declared rule.
    confidence = 1 - (1 - policy['confidence']) / (2 * len(rule_ids)) if policy['simultaneous'] else policy['confidence']
    if not 0.5 < confidence < 1:
        return rejected('POLICY_PRECISION_UNSUPPORTED')
    reasons, metrics = [], {}
    for rule in rule_ids:
        positive = [r for r in rows if r['human_gold'][rule] is True]
        negative = [r for r in rows if r['human_gold'][rule] is False]
        gold_unknown = len(rows) - len(positive) - len(negative)
        tp = sum(r['prediction'][rule] is True for r in positive)
        fp = sum(r['prediction'][rule] is True for r in negative)
        negative_unknown = sum(r['prediction'][rule] is None for r in negative)
        unknown = sum(r['prediction'][rule] is None for r in rows)
        recall = wilson_one_sided(tp, len(positive), confidence)
        false_positive = wilson_one_sided(fp + negative_unknown, len(negative), confidence)
        metrics[rule] = {'positive_groups': len(positive), 'negative_groups': len(negative),
                         'gold_unknown': gold_unknown, 'prediction_unknown': unknown,
                         'true_positive': tp, 'false_positive_observed': fp,
                         'false_positive_worst_case': fp + negative_unknown,
                         'recall_interval': recall, 'false_positive_interval': false_positive}
        if gold_unknown:
            reasons.append('GOLD_UNKNOWN:' + rule)
        if len(positive) < policy['min_positive_groups']:
            reasons.append('POSITIVE_SUPPORT_LOW:' + rule)
        if len(negative) < policy['min_negative_groups']:
            reasons.append('NEGATIVE_SUPPORT_LOW:' + rule)
        if recall is None or recall['lower'] < policy['min_recall_lower']:
            reasons.append('RECALL_LOWER_LOW:' + rule)
        if false_positive is None or false_positive['upper'] > policy['max_false_positive_upper']:
            reasons.append('FALSE_POSITIVE_UPPER_HIGH:' + rule)
        if unknown / max(1, len(rows)) > policy['max_unknown_rate']:
            reasons.append('UNKNOWN_RATE_HIGH:' + rule)
    return {'eligible': not reasons, 'scope': 'per_rule_severity', 'reasons': reasons,
            'source_groups': len(seen), 'per_rule': metrics,
            'policy': dict(policy), 'interval': 'one_sided_wilson',
            'effective_confidence': confidence,
            'note': '来源独立性与人工金标真实性须由应用证明；统计通过不是业务有效性证明。'}

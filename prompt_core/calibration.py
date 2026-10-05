"""Explicit audit gates; completing an audit never implies reliability."""
def calibration_decision(audit, policy):
    required = ("min_groups", "min_exact_rate", "max_abstain_rate")
    if not isinstance(policy, dict) or any(k not in policy for k in required):
        return {"eligible": False, "reasons": ["POLICY_NOT_FROZEN"]}
    n = audit.get("n", 0)
    if type(n) is not int or n < 0:
        return {"eligible": False, "reasons": ["AUDIT_INVALID"]}
    reasons = []
    if type(policy["min_groups"]) is not int or policy["min_groups"] < 5:
        return {"eligible": False, "reasons": ["POLICY_INVALID"]}
    for key in required[1:]:
        if type(policy[key]) not in (int, float) or not 0 <= policy[key] <= 1:
            return {"eligible": False, "reasons": ["POLICY_INVALID"]}
    if n < policy["min_groups"]:
        reasons.append("AUDIT_SUPPORT_LOW")
    dimensions = audit.get("per_dim", {})
    if not dimensions:
        reasons.append("DIMENSIONS_MISSING")
    for name, metric in dimensions.items():
        if any(type(metric.get(key, 0)) is not int or not 0 <= metric.get(key, 0) <= n
               for key in ("n", "abstain")):
            reasons.append("METRIC_INVALID:" + name)
            continue
        if metric.get("exact_rate", 0) is None or type(metric.get("exact_rate", 0)) not in (int, float) or not 0 <= metric.get("exact_rate", 0) <= 1:
            reasons.append("METRIC_INVALID:" + name)
            continue
        if metric.get("n", 0) + metric.get("abstain", 0) != n:
            reasons.append("GOLD_COVERAGE:" + name)
        if metric.get("exact_rate", 0) < policy["min_exact_rate"]:
            reasons.append("AGREEMENT_LOW:" + name)
        if metric.get("abstain", 0) / max(1, n) > policy["max_abstain_rate"]:
            reasons.append("ABSTENTION_HIGH:" + name)
    return {"eligible": not reasons, "reasons": reasons,
            "scope": "ordinal_score_agreement_only",
            "severe_admission": False,
            "note": "维度0分不是严重问题金标；严重风险验收仍需独立人工核验。"}

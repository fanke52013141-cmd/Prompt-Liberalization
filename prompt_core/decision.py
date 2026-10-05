"""Adoption eligibility is derived from evidence, never a UI boolean."""


def adoption_decision(improvement_supported, checks):
    """Missing/unknown checks fail closed; callers supply facts from owned storage."""
    required = ("binding", "evidence", "sample", "safety", "protocol")
    reasons = ["IMPROVEMENT_NOT_SUPPORTED"] if improvement_supported is not True else []
    reasons.extend("GATE_" + key.upper() for key in required if checks.get(key) is not True)
    return {"eligible": not reasons, "reasons": reasons, "policy_version": 1}

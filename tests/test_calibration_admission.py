from prompt_core.calibration import calibration_decision


def test_audit_without_policy_or_low_agreement_is_not_admitted():
    audit = {"n": 20, "per_dim": {"quality": {"n": 20, "abstain": 0, "exact_rate": 0.2}}}
    assert not calibration_decision(audit, None)["eligible"]
    policy = {"min_groups": 20, "min_exact_rate": 0.85, "max_abstain_rate": 0.1}
    assert not calibration_decision(audit, policy)["eligible"]
    audit["per_dim"]["quality"]["exact_rate"] = 0.9
    result = calibration_decision(audit, policy)
    assert result["eligible"]
    assert not result["severe_admission"]


def test_missing_gold_and_abstention_are_separate_gates():
    policy = {"min_groups": 5, "min_exact_rate": 0.8, "max_abstain_rate": 0.1}
    audit = {"n": 10, "per_dim": {"quality": {"n": 5, "abstain": 3, "exact_rate": 1}}}
    result = calibration_decision(audit, policy)
    assert "GOLD_COVERAGE:quality" in result["reasons"]
    assert "ABSTENTION_HIGH:quality" in result["reasons"]


def test_invalid_audit_cannot_appear_reliable():
    policy = {"min_groups": 5, "min_exact_rate": 0.8, "max_abstain_rate": 0.1}
    for metric in ({"n": 5, "abstain": -1, "exact_rate": 1},
                   {"n": 5, "abstain": 0, "exact_rate": float("nan")},
                   {"n": 5, "abstain": 0, "exact_rate": None}):
        assert not calibration_decision({"n": 5, "per_dim": {"q": metric}}, policy)["eligible"]

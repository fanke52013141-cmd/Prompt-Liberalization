import pytest

from prompt_core.protocol import estimate_sample_size, freeze_protocol, protocol_hash


def test_plan_is_explicit_and_hash_changes_with_criteria():
    data = dict(goal="fewer edits", usable_criterion="correct", severe_criterion="wrong result",
                sampling_description="independent tickets", planned_groups=50, independence_declared=True)
    frozen = freeze_protocol(data)
    assert frozen["stopping_rule"] == "fixed_sample"
    assert frozen["version"] == 2
    assert frozen["family_index"] == 1
    assert frozen["alpha_threshold"] == 0.025
    second = freeze_protocol(data, family_index=2)
    assert second["alpha"] == 0.05
    assert second["alpha_threshold"] == 0.0125
    assert second["multiplicity"] == "geometric_alpha_spending_per_prompt"
    assert sum(freeze_protocol(data, family_index=i)["alpha_threshold"] for i in range(1, 1000)) <= 0.05
    changed = dict(frozen, usable_criterion="complete")
    assert protocol_hash(frozen) != protocol_hash(changed)
    with pytest.raises(ValueError):
        freeze_protocol(dict(data, independence_declared=False))
    with pytest.raises(ValueError):
        freeze_protocol(dict(data, alpha=float("nan")))


def test_sample_planner_sensitivity_and_limits():
    small = estimate_sample_size(0.1, 0.3)
    large = estimate_sample_size(0.05, 0.3)
    assert large["suggested_groups"] > small["suggested_groups"]
    assert small["method"] == "normal_approximation"
    with pytest.raises(ValueError):
        estimate_sample_size(0.4, 0.2)

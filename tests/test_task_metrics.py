import pytest
from prompt_core.metrics import evaluate_metric, classification_summary


def test_exact_matching_has_no_implicit_normalization_or_missing_success():
    assert not evaluate_metric("answer ", "answer", {"type": "exact_match"})["usable"]
    assert evaluate_metric("answer ", "answer", {"type": "exact_match", "strip": True})["usable"]
    assert evaluate_metric("", None, {"type": "exact_match"})["status"] == "unknown"


def test_class_imbalance_missing_class_and_format_failure():
    config = {"type": "classification", "labels": ["negative", "positive"], "average": "macro"}
    results = [evaluate_metric("negative", "negative", config) for _ in range(9)]
    results += [evaluate_metric("negative", "positive", config), evaluate_metric("explanation", "positive", config)]
    micro = classification_summary(results, config["labels"], average="micro")
    macro = classification_summary(results, config["labels"], average="macro")
    assert micro["f1"] > macro["f1"]
    assert micro["format_failures"] == 1
    missing = classification_summary(results[:9], config["labels"], average="macro")
    assert missing["f1"] is None
    with pytest.raises(ValueError):
        classification_summary(results, config["labels"], average="binary")

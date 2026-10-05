from copy import deepcopy
from types import SimpleNamespace

import pytest

from prompt_gepa.adapter import PromptAdapter


def make_adapter(evaluator=None, stop=lambda: False):
    dev = {"id": "d", "purpose": "dev", "source_group": "g1", "input": "task", "reference": "gold"}
    select = {"id": "s", "purpose": "select", "source_group": "g2", "input": "select secret"}
    return PromptAdapter([dev], [select], evaluator or (lambda body, item: {
        "output": body, "score": 0.5, "status": "ok", "feedback": "missing detail"}),
        stop, SimpleNamespace), dev, select


def test_selection_can_score_but_never_reach_reflection():
    adapter, dev, select = make_adapter()
    batch = adapter.evaluate([select], {"body": "prompt"}, True)
    assert batch.scores == [0.5]
    with pytest.raises(ValueError, match="选择或封存"):
        adapter.make_reflective_dataset({"body": "prompt"}, batch, ["body"])
    batch = adapter.evaluate([dev], {"body": "prompt"}, True)
    feedback = adapter.make_reflective_dataset({"body": "prompt"}, batch, ["body"])
    assert "reference" not in feedback["body"][0]["Feedback"]
    assert "gold" not in str(feedback)


def test_unfrozen_or_sealed_case_rejected_before_call():
    called = []
    adapter, dev, _ = make_adapter(lambda *args: called.append(args))
    altered = {**dev, "input": "tampered"}
    for item in (altered, {**dev, "id": "sealed", "purpose": "test"}):
        with pytest.raises(ValueError):
            adapter.evaluate([item], {"body": "prompt"})
    assert called == []


def test_stop_checked_before_physical_callback_and_mutation_isolated():
    called = []
    adapter, dev, _ = make_adapter(lambda *args: called.append(args), lambda: True)
    with pytest.raises(InterruptedError):
        adapter.evaluate([dev], {"body": "prompt"})
    assert called == []
    original = deepcopy(dev)
    def evaluate(body, item):
        item["input"] = "mutated"
        return {"score": 0, "status": "generation_failure"}
    adapter, dev, _ = make_adapter(evaluate)
    batch = adapter.evaluate([dev], {"body": "prompt"}, True)
    assert dev == original
    assert batch.scores == [0]
    assert batch.trajectories[0]["case"]["input"] == "task"


def test_shared_source_and_nonbody_mutation_rejected():
    adapter, dev, select = make_adapter()
    with pytest.raises(ValueError, match="来源组"):
        PromptAdapter([dev], [{**select, "source_group": dev["source_group"]}], None, lambda: False)
    with pytest.raises(ValueError, match="正文"):
        adapter.evaluate([dev], {"body": "p", "model": "different"})

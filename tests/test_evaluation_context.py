import json

import pytest

from prompt_core.evaluation import validate_rubric_result
from prompt_lib.providers import has_severe_error


@pytest.mark.parametrize("scores", [{"correct": True}, {"correct": 4}, {}, {"correct": -1},
                                   {"correct": 3, "extra": 3}])
def test_invalid_scores_are_unknown(scores):
    result = validate_rubric_result({"scores": scores, "abstain": False, "severe": False}, ["correct"])
    assert result["abstain"] is True
    assert result["severe"] is None


def test_severity_is_not_a_keyword_check():
    assert has_severe_error("1+1=3", {"severe": True}) is True
    assert has_severe_error("注意避免判断错误", {"severe": False}) is False
    assert has_severe_error("1+1=3", {"scores": {"correct": 0}}) is None


def test_judge_receives_input_reference_and_all_anchors(client, monkeypatch):
    from .conftest import setup_project_with_data
    from prompt_lib import engine
    from prompt_lib.providers import CallResult
    s = setup_project_with_data(client)
    captured = []

    def respond(*args, **kwargs):
        captured.append(args[3])
        return CallResult('{"scores":{},"abstain":true,"severe":null}', "stop", {"in": 1, "out": 1})

    monkeypatch.setattr(engine, "call_model", respond)
    engine.evaluate_once("答复", s["rubric_id"], {"connection_id": "conn_mock"},
                         task_input={"question": "1+1?"}, evaluation_reference={"answer": "2"})
    messages = captured[0]
    assert "1+1?" in messages[1]["content"]
    assert '"answer": "2"' in messages[1]["content"]
    from prompt_lib.db import get_db
    schema = json.loads(get_db().one("SELECT schema_json FROM rubrics WHERE id=?", (s["rubric_id"],))[0])
    for dimension in schema["dimensions"]:
        for anchor in dimension["anchors"].values():
            assert anchor in messages[0]["content"]


def test_deterministic_metric_does_not_call_model_and_missing_reference_abstains(client, monkeypatch):
    from .conftest import setup_project_with_data
    from prompt_lib import engine
    from prompt_lib.db import get_db
    s = setup_project_with_data(client)
    db = get_db()
    schema = json.loads(db.one("SELECT schema_json FROM rubrics WHERE id=?", (s["rubric_id"],))[0])
    schema["dimensions"] = schema["dimensions"][:1]
    db.execute("UPDATE rubrics SET schema_json=? WHERE id=?", (json.dumps(schema), s["rubric_id"]))
    def forbidden(*args, **kwargs):
        raise AssertionError("deterministic evaluation must not call the provider")
    monkeypatch.setattr(engine, "call_model", forbidden)
    config = {"metric": {"type": "exact_match", "reference_field": "answer"}}
    result = engine.evaluate_once("2", s["rubric_id"], config, evaluation_reference={"answer": "2"})
    assert not result["abstain"]
    assert list(result["scores"].values()) == [3]
    assert result["severe"] is None
    result = engine.evaluate_once("2", s["rubric_id"], config, evaluation_reference={})
    assert result["abstain"] and result["error"] == "missing_reference"

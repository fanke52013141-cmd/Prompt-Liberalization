import json

from prompt_lib.engine import render_messages


def test_frozen_constraints_are_sent_and_reference_is_excluded():
    prompt = {"body": "答题：{{question}}", "variables": ["question"],
              "frozen_segments": [{"name": "facts", "text": "不得虚构事实"}]}
    item = {"runtime_input": {"question": "题目", "expert_answer": "hidden"},
            "evaluation_only": {"expert_answer": "sentinel"}}
    messages = render_messages(prompt, item, "task")
    blob = json.dumps(messages, ensure_ascii=False)
    assert messages[0]["content"].count("不得虚构事实") == 1
    assert "hidden" not in blob and "sentinel" not in blob


def test_input_cannot_expand_another_variable():
    prompt = {"body": "{{a}} / {{b}}", "variables": ["a", "b"]}
    messages = render_messages(prompt, {"runtime_input": {"a": "{{b}}", "b": "actual"}}, "task")
    assert messages[0]["content"] == "{{b}} / actual"

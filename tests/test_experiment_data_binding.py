import pytest

from .conftest import setup_project_with_data, start_run
from prompt_lib import runs
from prompt_lib.core import BizError
from prompt_lib.db import get_db


@pytest.mark.parametrize("column,value", [
    ("runtime_input_json", '{"input":"changed input"}'),
    ("evaluation_only_json", '{"reference":"changed answer"}'),
    ("source_group_id", "different-source"),
    ("split", "sealed"),
])
def test_changed_case_blocks_resume_before_dispatch(client, monkeypatch, column, value):
    data = setup_project_with_data(client)

    def pause(self, *args, **kwargs):
        raise BizError("BUDGET_EXHAUSTED", "pause after baseline and candidate scoring")

    monkeypatch.setattr(runs.RunService, "_persist_progress", pause)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:4], max_candidates=1)
    assert run["state"] == "paused_budget", run
    db = get_db()
    count = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (run["id"],))["n"]
    item_id = run["snapshot"]["data"]["reflection_item_ids"][0]
    binding = run["snapshot"]["data"]["case_bindings"][item_id]
    assert set(binding) == {"split", "content_hash"}
    # Column names are fixed by the parametrization, never supplied by an API.
    db.execute(f"UPDATE dataset_items SET {column}=? WHERE id=?", (value, item_id))
    response = client.post(f"/workflow-api/v1/runs/{run['id']}/resume")
    assert response.status_code == 409, response.text
    assert "DATA_SNAPSHOT_CHANGED" in response.text
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (run["id"],))["n"] == count
    assert db.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"] == "paused_budget"


def test_change_during_generation_stops_before_evaluation(client, monkeypatch):
    from prompt_lib import engine
    data = setup_project_with_data(client)
    actual_generate = engine.generate_once

    def generate(db, project, prompt, item, *args, **kwargs):
        output = actual_generate(db, project, prompt, item, *args, **kwargs)
        db.execute("UPDATE dataset_items SET evaluation_only_json=? WHERE id=?",
                   ('{"reference":"changed during request"}', item["id"]))
        return output

    monkeypatch.setattr(engine, "generate_once", generate)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:4], max_candidates=1)
    assert run["state"] == "failed", run
    assert "参考" in run["error"]
    calls = get_db().query("SELECT role FROM ledger WHERE run_id=?", (run["id"],))
    assert [row["role"] for row in calls] == ["generation"]

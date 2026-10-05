from prompt_lib.engine import decide_keep


def test_candidate_cannot_win_by_losing_evaluation_coverage():
    previous = {"score": 2, "severe": 0, "length": 100, "evaluation_coverage": 1}
    candidate = {"score": 3, "severe": 0, "length": 100, "evaluation_coverage": 0.5}
    assert decide_keep(previous, candidate, 0.01)["decision"] == "discarded"
    candidate["evaluation_coverage"] = 1
    assert decide_keep(previous, candidate, 0.01)["decision"] == "kept"


def test_dev_improvement_cannot_override_selection_regression(client, monkeypatch):
    from .conftest import setup_project_with_data, wait_run
    from prompt_lib import runs
    s = setup_project_with_data(client)
    selection = s["item_ids"][8:10]
    actual = runs.score_prompt
    def scored(db, project, prompt, ids, *args, **kwargs):
        if ids != selection:
            return actual(db, project, prompt, ids, *args, **kwargs)
        baseline = prompt["id"] == s["prompt_id"]
        return {"score": 3 if baseline else 0, "usable_rate": 1 if baseline else 0,
                "evaluation_coverage": 1, "severe": 0, "items": [{"item_id": i, "usable": baseline} for i in ids]}
    monkeypatch.setattr(runs, "score_prompt", scored)
    api = "/workflow-api/v1"
    manifest = client.get(f"{api}/projects/{s['pid']}/manifests").json()["manifests"][0]["id"]
    draft = {"mode": "explore", "prompt": {"baseline_id": s["prompt_id"]}, "rubric_id": s["rubric_id"],
             "manifest_id": manifest, "data": {"dev_item_ids": s["item_ids"][:4], "select_item_ids": selection},
             "models": {role: {"connection_id": "conn_mock"} for role in ("generation", "evaluation", "optimizer")},
             "optimization": {"max_candidates": 1, "dev_sample_size": 4, "min_delta": 0.02},
             "budget": {"mode": "token", "total_limit": 10000000, "search_limit": 8000000, "acceptance_limit": 1000000}}
    response = client.post(f"{api}/projects/{s['pid']}/runs", json=draft)
    assert response.status_code == 202, response.text
    run = wait_run(client, response.json()["id"])
    assert run["state"] == "completed", run
    assert run["candidates"]
    candidate = run["candidates"][0]
    assert candidate["decision"] == "discarded"
    assert not candidate["selection"]["passed"]
    assert candidate["selection"]["regressions"] == 2
    assert candidate["selection"]["purpose"] == "search_selection_not_final_proof"

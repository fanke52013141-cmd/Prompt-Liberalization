from .conftest import setup_project_with_data, start_run, wait_run


def paused_run(client):
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0,
                    budget={"mode": "token", "total_limit": 3000,
                            "search_limit": 2000, "acceptance_limit": 1000})
    assert run["state"] == "paused_budget"
    return run


def test_explicit_budget_increase_resumes_and_preserves_execution_snapshot(client):
    run = paused_run(client)
    new_limits = {"total_limit": 100000, "search_limit": 90000, "acceptance_limit": 1000}
    result = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget",
                          json={"revision": run["revision"], "limits": new_limits})
    assert result.status_code == 200, result.text
    updated = result.json()
    assert updated["snapshot_hash"] == run["snapshot_hash"]
    assert updated["budget"]["reserved"] == run["budget"]["reserved"]
    assert updated["revision"] == run["revision"] + 1
    events = client.get(f"/workflow-api/v1/runs/{run['id']}/events").json()["events"]
    assert any(event["type"] == "budget_increased" and event["payload"]["after"] == new_limits for event in events)
    assert client.post(f"/workflow-api/v1/runs/{run['id']}/resume").status_code == 200
    completed = wait_run(client, run["id"])
    assert completed["state"] == "completed", completed
    assert completed["budget"]["spent"]["search"] > 0


def test_budget_increase_rejects_stale_revision_and_lower_acceptance_reserve(client):
    run = paused_run(client)
    lower = {"total_limit": 100000, "search_limit": 90000, "acceptance_limit": 500}
    result = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget",
                          json={"revision": run["revision"], "limits": lower})
    assert result.status_code == 422
    assert result.json()["code"] == "BUDGET_CANNOT_DECREASE"
    higher = {**lower, "acceptance_limit": 1000}
    assert client.patch(f"/workflow-api/v1/runs/{run['id']}/budget",
                        json={"revision": run["revision"], "limits": higher}).status_code == 200
    stale = client.patch(f"/workflow-api/v1/runs/{run['id']}/budget",
                         json={"revision": run["revision"], "limits": higher})
    assert stale.status_code == 409
    assert stale.json()["code"] == "REVISION_CONFLICT"

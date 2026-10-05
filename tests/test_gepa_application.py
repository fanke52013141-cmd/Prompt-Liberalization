import pytest
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .conftest import setup_project_with_data, start_run, wait_run
from prompt_lib.core import BizError
from prompt_lib.db import get_db
from prompt_lib.domain import ProjectsService
from prompt_lib.gepa_search import ApplicationSearch
from prompt_lib.ledger import BudgetState, Ledger
from prompt_lib.engine import call_model


def setup_search(client, budget=None):
    data = setup_project_with_data(client)
    run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                    dev_ids=data["item_ids"][:2], max_candidates=0)
    snapshot = run["snapshot"]
    snapshot["data"]["select_item_ids"] = data["item_ids"][8:10]
    db = get_db()
    ledger = Ledger(db)
    state = BudgetState(budget or run["budget"])
    if budget:
        import json
        db.execute("UPDATE runs SET budget_state_json=? WHERE id=?", (json.dumps(state.to_json()), run["id"]))
    search = ApplicationSearch(db, ProjectsService(db).get(data["pid"]), snapshot,
                               run["id"], state, ledger, lambda: False)
    return search, db


def test_generation_evaluation_reflection_share_application_ledger(client):
    search, db = setup_search(client)
    before = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (search.run_id,))["n"]
    case = next(iter(search.adapter.train.values()))
    result = search.evaluate(search.baseline["body"], case)
    assert "output_id" in result
    output_id = result["output_id"]
    messages = [{"role": "user", "content": "propose revised instruction"}]
    search.reflect(messages)
    search.evaluate(search.baseline["body"], case)
    search.reflect(messages)
    roles = [row["role"] for row in db.query(
        "SELECT role FROM ledger WHERE run_id=? ORDER BY created_at,id", (search.run_id,))]
    assert len(roles) == before + 1
    assert {"generation", "evaluation", "optimizer"} <= set(roles)
    assert db.one("SELECT id FROM outputs WHERE run_id=? AND id=?", (search.run_id, output_id))


def test_budget_blocks_generation_and_reflection_before_provider_dispatch(client):
    search, db = setup_search(client, {"mode": "token", "total_limit": 1,
                                     "search_limit": 1, "acceptance_limit": 0})
    before = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (search.run_id,))["n"]
    with pytest.raises(BizError) as error:
        search.evaluate(search.baseline["body"] + "请按步骤解释。",
                        next(iter(search.adapter.train.values())))
    assert error.value.code == "BUDGET_EXHAUSTED"
    with pytest.raises(BizError) as error:
        search.reflect([{"role": "user", "content": "rewrite"}])
    assert error.value.code == "BUDGET_EXHAUSTED"
    assert db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (search.run_id,))["n"] == before


def test_sdk_swallowed_budget_error_still_pauses_application_run(client):
    search, _ = setup_search(client, {"mode": "token", "total_limit": 1,
                                     "search_limit": 1, "acceptance_limit": 0})

    def swallowing_sdk(*args, **kwargs):
        try:
            args[2]([{"role": "user", "content": "rewrite"}])
        except BizError:
            pass
        return {}

    with patch("prompt_lib.gepa_search.optimize", side_effect=swallowing_sdk):
        with pytest.raises(BizError) as error:
            search.run(1)
    assert error.value.code == "BUDGET_EXHAUSTED"


def test_gepa_candidate_over_length_limit_is_rejected_before_model_calls(client):
    search, db = setup_search(client)
    search.snapshot["optimization"] = {"length_limit_chars": len(search.baseline["body"])}
    before = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (search.run_id,))["n"]
    result = search.evaluate(search.baseline["body"] + "超出限制",
                             next(iter(search.adapter.train.values())))
    after = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (search.run_id,))["n"]
    assert result["status"] == "candidate_rejected_length"
    assert before == after


def test_output_allowance_is_reserved_before_provider_dispatch(client):
    from unittest.mock import patch
    budget = BudgetState({"mode": "token", "total_limit": 500,
                          "search_limit": 500, "acceptance_limit": 0})
    db = get_db()
    with patch("prompt_lib.engine._get_provider") as provider:
        with pytest.raises(BizError) as error:
            call_model(db, "generation", {"connection_id": "conn_mock"},
                       [{"role": "user", "content": "x"}], {"max_tokens": 1000},
                       "output-cap-test", "a", "search", budget, Ledger(db))
        assert error.value.code == "BUDGET_EXHAUSTED"
        provider.return_value.complete.assert_not_called()


def test_gepa_strategy_runs_through_run_api_and_saves_lineage(client):
    data = setup_project_with_data(client)
    manifests = client.get(f"/workflow-api/v1/projects/{data['pid']}/manifests").json()["manifests"]
    db = get_db()
    seen_reflection = []

    class Batch:
        def __init__(self, outputs, scores, trajectories=None):
            self.outputs, self.scores, self.trajectories = outputs, scores, trajectories

    def fake_optimize(seed_candidate, trainset, valset, adapter, reflection_lm, **kwargs):
        initial = adapter.evaluate(trainset[:2], seed_candidate, capture_traces=True)
        reflective_data = adapter.make_reflective_dataset(seed_candidate, initial, ["body"])
        seen_reflection.append(str(reflective_data))
        reflection_lm([{"role": "user", "content": "propose a grounded revision"}])
        candidate = {"body": seed_candidate["body"] + "\n请用清楚的小标题组织点评。"}
        for item in trainset:
            adapter.evaluate([item], candidate)
        base_val = adapter.evaluate(valset, seed_candidate)
        candidate_val = adapter.evaluate(valset, candidate)
        return SimpleNamespace(
            candidates=[seed_candidate, candidate], parents=[[None], [0]],
            val_subscores=[dict(zip([x["id"] for x in valset], base_val.scores)),
                           dict(zip([x["id"] for x in valset], candidate_val.scores))],
            per_val_instance_best_candidates={x["id"]: {1} for x in valset},
            best_idx=1, total_metric_calls=len(trainset) + 2 * len(valset))

    sdk = SimpleNamespace(EvaluationBatch=Batch, optimize=fake_optimize)
    with patch("prompt_gepa.adapter.load_sdk", return_value=sdk), \
         patch("prompt_gepa.search.load_sdk", return_value=sdk):
        draft = {
            "mode": "explore", "prompt": {"baseline_id": data["prompt_id"]},
            "manifest_id": manifests[0]["id"], "rubric_id": data["rubric_id"],
            "data": {"dev_item_ids": data["item_ids"][:3],
                     "select_item_ids": data["item_ids"][8:10]},
            "models": {role: {"connection_id": "conn_mock"}
                       for role in ("generation", "evaluation", "optimizer")},
            "optimization": {"strategy": "gepa", "gepa_max_metric_calls": 10,
                             "dev_sample_size": 2, "min_delta": 0.02},
            "budget": {"mode": "token", "total_limit": 10_000_000,
                       "search_limit": 8_000_000, "acceptance_limit": 1_000_000}}
        invalid = {**draft, "optimization": {**draft["optimization"], "human_in_loop": True}}
        validation = client.post(
            f"/workflow-api/v1/projects/{data['pid']}/runs/validate", json=invalid)
        assert validation.status_code == 422
        assert "optimization.human_in_loop" in validation.json()["field_errors"]
        response = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs", json=draft)
        assert response.status_code == 202, response.text
        result = wait_run(client, response.json()["id"])

    assert result["state"] == "completed", result.get("error")
    assert result["snapshot"]["package_lock"]["gepa"] == "0.1.4"
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["candidate_id"] == "gepa_1"
    assert candidate["parent_candidate_ids"] == [data["prompt_id"]]
    assert candidate["gepa"]["parents"] == [0]
    version = db.one("SELECT parent_id,origin FROM prompt_versions WHERE id=?",
                     (candidate["prompt_version_id"],))
    assert version["parent_id"] == data["prompt_id"]
    assert version["origin"] == "gepa"
    assert "参考1" not in seen_reflection[0]
    roles = {r["role"] for r in db.query("SELECT DISTINCT role FROM ledger WHERE run_id=?", (result["id"],))}
    assert {"generation", "evaluation", "optimizer"} <= roles


@pytest.mark.skipif(importlib.util.find_spec("gepa") is None,
                    reason="可选GEPA扩展未安装")
def test_pinned_gepa_sdk_search_and_run_dir_resume_without_duplicate_calls(client):
    data = setup_project_with_data(client)
    base = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                     dev_ids=data["item_ids"][:2], max_candidates=0)
    snapshot = base["snapshot"]
    snapshot["data"]["select_item_ids"] = data["item_ids"][8:10]
    db = get_db()
    budget, ledger = BudgetState(base["budget"]), Ledger(db)
    run_dir = Path(db.path).parent / ".gepa" / base["id"]
    search = ApplicationSearch(db, ProjectsService(db).get(data["pid"]), snapshot,
                               base["id"], budget, ledger, lambda: False, run_dir=run_dir)
    reflect = search.reflect
    observed = []

    def inspect_reflection(messages):
        observed.append(str(messages))
        return reflect(messages)

    search.reflect = inspect_reflection
    result = search.run(8, seed=41)
    assert result["strategy"] == "gepa"
    assert result["sdk_version"] == "0.1.4"
    assert result["candidates"]
    assert len(result["parents"]) == len(result["candidates"])
    assert result["best_idx"] < len(result["candidates"])
    assert run_dir.exists()
    assert all("参考" not in message for message in observed)

    attempts_before = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (base["id"],))["n"]
    resumed = ApplicationSearch(db, ProjectsService(db).get(data["pid"]), snapshot,
                                base["id"], BudgetState(base["budget"]), Ledger(db),
                                lambda: False, run_dir=run_dir).run(8, seed=41)
    attempts_after = db.one("SELECT COUNT(*) n FROM ledger WHERE run_id=?", (base["id"],))["n"]
    assert resumed["best_idx"] == result["best_idx"]
    assert attempts_after == attempts_before


@pytest.mark.skipif(importlib.util.find_spec("gepa") is None,
                    reason="可选GEPA扩展未安装")
def test_pinned_gepa_runs_through_experiment_api_offline(client):
    data = setup_project_with_data(client)
    manifests = client.get(f"/workflow-api/v1/projects/{data['pid']}/manifests").json()["manifests"]
    draft = {
        "mode": "explore", "prompt": {"baseline_id": data["prompt_id"]},
        "manifest_id": manifests[0]["id"], "rubric_id": data["rubric_id"],
        "data": {"dev_item_ids": data["item_ids"][:3],
                 "select_item_ids": data["item_ids"][8:10]},
        "models": {role: {"connection_id": "conn_mock"}
                   for role in ("generation", "evaluation", "optimizer")},
        "optimization": {"strategy": "gepa", "gepa_max_metric_calls": 8,
                         "dev_sample_size": 2, "min_delta": 0.02},
        "budget": {"mode": "token", "total_limit": 10_000_000,
                   "search_limit": 8_000_000, "acceptance_limit": 1_000_000}}
    response = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs", json=draft)
    assert response.status_code == 202, response.text
    result = wait_run(client, response.json()["id"])

    assert result["state"] == "completed", result.get("error")
    assert result["snapshot"]["package_lock"]["gepa"] == "0.1.4"
    assert result["stage"] == "done"
    assert all(candidate["gepa"]["sdk_version"] == "0.1.4"
               for candidate in result["candidates"])
    assert all(set(candidate["gepa"]["frontier_cases"]) <= set(data["item_ids"][8:10])
               for candidate in result["candidates"])
    roles = {r["role"] for r in get_db().query(
        "SELECT DISTINCT role FROM ledger WHERE run_id=?", (result["id"],))}
    assert {"generation", "evaluation", "optimizer"} <= roles


@pytest.mark.skipif(importlib.util.find_spec("gepa") is None,
                    reason="可选GEPA扩展未安装")
def test_pinned_gepa_budget_pause_resumes_same_checkpoint(client):
    data = setup_project_with_data(client)
    manifests = client.get(f"/workflow-api/v1/projects/{data['pid']}/manifests").json()["manifests"]
    draft = {
        "mode": "explore", "prompt": {"baseline_id": data["prompt_id"]},
        "manifest_id": manifests[0]["id"], "rubric_id": data["rubric_id"],
        "data": {"dev_item_ids": data["item_ids"][:3],
                 "select_item_ids": data["item_ids"][8:10]},
        "models": {role: {"connection_id": "conn_mock"}
                   for role in ("generation", "evaluation", "optimizer")},
        "optimization": {"strategy": "gepa", "gepa_max_metric_calls": 8,
                         "dev_sample_size": 2, "min_delta": 0.02},
        "budget": {"mode": "token", "total_limit": 100_000,
                       "search_limit": 10_000, "acceptance_limit": 20_000}}
    response = client.post(f"/workflow-api/v1/projects/{data['pid']}/runs", json=draft)
    assert response.status_code == 202, response.text
    paused = wait_run(client, response.json()["id"])
    assert paused["state"] == "paused_budget", f"state={paused['state']}; error={paused.get('error')!r}; budget={paused['budget']!r}"
    events = client.get(f"/workflow-api/v1/runs/{paused['id']}/events").json()["events"]
    assert any(event["type"] == "stage" and event["payload"].get("stage") == "gepa_search"
               for event in events)
    assert any(event["type"] == "gepa_evaluation" for event in events)
    attempts_before = get_db().one("SELECT COUNT(*) n FROM ledger WHERE run_id=?",
                                   (paused["id"],))["n"]

    increased = client.patch(f"/workflow-api/v1/runs/{paused['id']}/budget", json={
        "revision": paused["revision"],
        "limits": {"total_limit": 2_000_000, "search_limit": 1_800_000,
                   "acceptance_limit": 200_000}})
    assert increased.status_code == 200, increased.text
    resumed = client.post(f"/workflow-api/v1/runs/{paused['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    completed = wait_run(client, paused["id"])
    assert completed["state"] == "completed", completed.get("error")
    logical_duplicates = get_db().query(
        "SELECT logical_id FROM ledger WHERE run_id=? GROUP BY logical_id HAVING COUNT(*)>1",
        (paused["id"],))
    assert not logical_duplicates
    attempts_after = get_db().one("SELECT COUNT(*) n FROM ledger WHERE run_id=?",
                                  (paused["id"],))["n"]
    assert attempts_after >= attempts_before
    assert completed["snapshot"]["package_lock"]["gepa"] == "0.1.4"

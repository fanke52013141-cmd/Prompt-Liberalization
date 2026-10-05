from prompt_core.reflection import sample_reflection


def test_sampling_covers_distinct_errors_and_is_reproducible():
    items = [{"item_id": str(i), "gen_status": "ok", "dims": {"correct": 0}, "usable": False}
             for i in range(10)]
    items.append({"item_id": "format", "gen_status": "ok", "dims": {"format": 0}, "usable": False})
    first, metadata = sample_reflection(items, 2, 42)
    second, repeated = sample_reflection(list(reversed(items)), 2, 42)
    assert [i["item_id"] for i in first] == [i["item_id"] for i in second]
    assert metadata == repeated
    assert "dimension:format" in metadata["covered_labels"]
    assert "dimension:correct" in metadata["covered_labels"]
    assert not metadata["causal_claim"]


def test_unknown_and_generation_failures_are_distinct_observations():
    items = [{"item_id": "failed", "gen_status": "error", "usable": False},
             {"item_id": "unknown", "gen_status": "ok", "eval_abstain": True},
             {"item_id": "correct", "gen_status": "ok", "usable": True, "dims": {"quality": 3}}]
    picked, metadata = sample_reflection(items, 5, 1)
    assert len(picked) == 2
    assert {"generation_failure", "evaluation_unknown"} <= set(metadata["covered_labels"])

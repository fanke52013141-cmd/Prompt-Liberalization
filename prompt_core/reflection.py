"""Seeded greedy coverage sampling; labels describe observations, not causes."""
import random


def sample_reflection(items, limit, seed):
    if type(limit) is not int or limit < 1:
        raise ValueError("reflection limit must be a positive integer")
    pool = sorted(items, key=lambda item: item["item_id"])
    random.Random(seed).shuffle(pool)
    def labels(item):
        result = {"dimension:" + str(k) for k, v in item.get("dims", {}).items()
                  if type(v) is int and v <= 1}
        if item.get("severe") is True:
            result.add("severe_observed")
        if item.get("gen_status") != "ok":
            result.add("generation_failure")
        if item.get("eval_abstain"):
            result.add("evaluation_unknown")
        if item.get("usable") is False:
            result.add("unusable")
        return result
    remaining = [item for item in pool if labels(item)]
    selected, covered = [], set()
    while remaining and len(selected) < limit:
        best = max(remaining, key=lambda item: len(labels(item) - covered))
        selected.append(best)
        covered.update(labels(best))
        remaining.remove(best)
    return selected, {"seed": seed, "item_ids": [item["item_id"] for item in selected],
                      "covered_labels": sorted(covered), "eligible_count": sum(bool(labels(item)) for item in pool),
                      "sampling": "seeded_error_coverage", "causal_claim": False}

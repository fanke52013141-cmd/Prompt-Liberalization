"""Run the pinned official engine using application-owned model callbacks."""
from copy import deepcopy
from pathlib import Path

from .adapter import load_sdk


def optimize(body, adapter, reflection_lm, *, max_metric_calls, seed=0, run_dir=None):
    adapter._body({"body": body})
    if not callable(reflection_lm):
        raise ValueError("反思模型必须是经过应用预算账本的调用函数。")
    if isinstance(max_metric_calls, bool) or not isinstance(max_metric_calls, int) or max_metric_calls < 1:
        raise ValueError("评价调用上限必须为正整数。")
    def reflect(messages):
        if adapter.stop_signal():
            raise InterruptedError("优化已取消；未执行反思调用。")
        return reflection_lm(messages)
    sdk = load_sdk()
    result = sdk.optimize(
        seed_candidate={"body": body}, trainset=deepcopy(list(adapter.train.values())),
        valset=deepcopy(list(adapter.selection.values())), adapter=adapter,
        reflection_lm=reflect, candidate_selection_strategy="pareto",
        frontier_type="instance", use_merge=False, max_metric_calls=max_metric_calls,
        stop_callbacks=lambda state: adapter.stop_signal(), seed=seed,
        cache_evaluation=False, use_wandb=False, use_mlflow=False,
        display_progress_bar=False, raise_on_exception=True,
        run_dir=str(Path(run_dir)) if run_dir else None)
    def case_id(key):
        if key in adapter.selection:
            return str(key)
        if isinstance(key, int) and not isinstance(key, bool):
            ordered = list(adapter.selection)
            if 0 <= key < len(ordered):
                return ordered[key]
        if isinstance(key, str) and key.isdecimal():
            ordered = list(adapter.selection)
            index = int(key)
            if 0 <= index < len(ordered):
                return ordered[index]
        return str(key)

    selection_scores = [{case_id(key): float(value) for key, value in scores.items()}
                        for scores in result.val_subscores]
    return {"strategy": "gepa", "sdk_version": "0.1.4", "seed": seed,
            "candidates": deepcopy(result.candidates), "parents": deepcopy(result.parents),
            "selection_scores": selection_scores,
            "frontier": {case_id(key): sorted(value)
                         for key, value in result.per_val_instance_best_candidates.items()},
            "best_idx": result.best_idx, "metric_calls": result.total_metric_calls,
            "evaluations": deepcopy(adapter.records)}

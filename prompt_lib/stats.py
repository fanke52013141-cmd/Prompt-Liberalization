"""配对统计（D09 / TC044—TC047）。

实现配对百分位 bootstrap（按来源成组重采样）、精确 McNemar 检验、
零事件单侧置信上界与缺失值界限。不依赖 SciPy，纯标准库，可离线复算。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict


def exact_mcnemar(b: int, c: int) -> float:
    """精确 McNemar 双侧 p 值。

    b=仅候选可用(修复)、c=仅基线可用(退步)。在 n=b+c、p=0.5 的二项分布下
    p = 2 * P(X <= min(b,c))（对称截断到 1）。
    FX07 黄金值：b=20, c=4 -> 约 0.00154388。
    """
    if b + c == 0:
        return 1.0
    n = b + c
    k = min(b, c)

    def log_pmf(i: int) -> float:
        return math.log(2.0) * (-n) + math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)

    tail = sum(math.exp(log_pmf(i)) for i in range(0, k + 1))
    p = min(1.0, 2.0 * tail)
    return p


def paired_bootstrap(values_baseline: list[list[float]], values_candidate: list[list[float]],
                     n_resamples: int = 20000, seed: int = 20260927,
                     alpha: float = 0.05) -> dict:
    """按来源成组重采样的配对 bootstrap。

    values_*[i] 是第 i 个来源的一个或多个输出指标（先在来源内平均，再对来源重采样），
    避免把同一来源的多次输出当独立样本（TC045）。
    返回配对差异（候选-基线）的均值与百分位区间。
    """
    if len(values_baseline) != len(values_candidate):
        raise ValueError("配对来源数不一致")
    group_means = []
    for bl, cl in zip(values_baseline, values_candidate):
        mb = sum(bl) / len(bl) if bl else 0.0
        mc = sum(cl) / len(cl) if cl else 0.0
        group_means.append((mb, mc))
    n = len(group_means)
    if n == 0:
        return {"mean_diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "group_n": 0, "degenerate": True}
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_resamples):
        sb = sc = 0.0
        for _ in range(n):
            mb, mc = group_means[rng.randrange(n)]
            sb += mb
            sc += mc
        diffs.append((sc - sb) / n)
    diffs.sort()
    # 全部差异相同属退化情形：标记而非假装有区间（TC047）
    degenerate = diffs[0] == diffs[-1]
    lo = diffs[max(0, int(math.floor((alpha / 2) * n_resamples)))]
    hi = diffs[min(n_resamples - 1, int(math.ceil((1 - alpha / 2) * n_resamples)) - 1)]
    mean_obs = sum(mc for _, mc in group_means) / n - sum(mb for mb, _ in group_means) / n
    return {"mean_diff": mean_obs, "ci_low": lo, "ci_high": hi, "group_n": n,
            "degenerate": degenerate}


def zero_event_upper_bound(n: int, alpha: float = 0.05) -> float:
    """零严重事件的单侧置信上界（Clopper-Pearson）。

    0/n 时上界 = 1 - alpha^(1/n)；n=100, alpha=0.05 -> 约 0.0295 (2.95%)。
    不得宣称真实错误率为 0（TC047）。
    """
    if n <= 0:
        return 1.0
    return 1.0 - alpha ** (1.0 / n)


def missing_bounds(usable: int, unusable: int, unknown: int) -> dict:
    """缺失值界限（FX08）：unknown 全好/全坏两种情形给出区间。

    80条中50可用/20不可用/10未知 -> [62.5%, 75%]，不是 50/70。
    """
    total = usable + unusable + unknown
    if total == 0:
        return {"low": 0.0, "high": 0.0, "total": 0}
    return {"low": usable / total, "high": (usable + unknown) / total, "total": total,
            "unknown_n": unknown}


def group_outcomes(pairs: list[dict]) -> tuple[int, int, int, int]:
    """把逐来源二元配对结果汇总为 (both, candidate_only, baseline_only, neither)。"""
    agg = defaultdict(lambda: [0, 0])  # group -> [baseline_usable_count, candidate_usable_count]
    for p in pairs:
        g = agg[p["group"]]
        if p.get("baseline_usable"):
            g[0] += 1
        if p.get("candidate_usable"):
            g[1] += 1
    both = cand_only = base_only = neither = 0
    for g, (b, c) in agg.items():
        outs = g_outcomes = (b > 0, c > 0)
        if outs == (True, True):
            both += 1
        elif outs == (False, True):
            cand_only += 1
        elif outs == (True, False):
            base_only += 1
        else:
            neither += 1
    return both, cand_only, base_only, neither

"""统计黄金值验收（TC044/TC045/TC047，夹具 FX07/FX08）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_lib.stats import (exact_mcnemar, missing_bounds, paired_bootstrap,
                              zero_event_upper_bound)


def fx07_pairs():
    """58双可用 / 20仅候选(修复) / 4仅基线(退步) / 18双不可用，共100独立来源。"""
    pairs = []
    for _ in range(58):
        pairs.append({"group": f"g{len(pairs)}", "baseline_usable": True, "candidate_usable": True})
    for _ in range(20):
        pairs.append({"group": f"g{len(pairs)}", "baseline_usable": False, "candidate_usable": True})
    for _ in range(4):
        pairs.append({"group": f"g{len(pairs)}", "baseline_usable": True, "candidate_usable": False})
    for _ in range(18):
        pairs.append({"group": f"g{len(pairs)}", "baseline_usable": False, "candidate_usable": False})
    return pairs


def test_fx07_golden_rates():
    pairs = fx07_pairs()
    base = sum(1 for p in pairs if p["baseline_usable"]) / len(pairs)
    cand = sum(1 for p in pairs if p["candidate_usable"]) / len(pairs)
    assert round(base * 100) == 62
    assert round(cand * 100) == 78
    assert round((cand - base) * 100) == 16
    fix = sum(1 for p in pairs if p["candidate_usable"] and not p["baseline_usable"])
    regress = sum(1 for p in pairs if p["baseline_usable"] and not p["candidate_usable"])
    assert (fix, regress) == (20, 4)


def test_fx07_mcnemar_golden():
    # 验收方案给定约 0.0015438795；本实现为精确二项 2*P(X<=4|n=24)
    p = exact_mcnemar(20, 4)
    assert abs(p - 0.0015438795) < 1e-6
    assert abs(p - 25902 / 16777216) < 1e-12


def test_fx07_bootstrap_around_reference_interval():
    pairs = fx07_pairs()
    bl = [[1.0 if p["baseline_usable"] else 0.0] for p in pairs]
    cl = [[1.0 if p["candidate_usable"] else 0.0] for p in pairs]
    r = paired_bootstrap(bl, cl, n_resamples=20000, seed=20260927)
    assert abs(r["mean_diff"] - 0.16) < 1e-9
    # 文档演示区间约 [0.07, 0.25]；不要求逐bit一致，只约束在邻域内
    assert 0.05 <= r["ci_low"] <= 0.10
    assert 0.22 <= r["ci_high"] <= 0.28
    assert r["group_n"] == 100
    assert not r["degenerate"]


def test_fx08_missing_bounds():
    mb = missing_bounds(50, 20, 10)
    assert abs(mb["low"] - 0.625) < 1e-12
    assert abs(mb["high"] - 0.75) < 1e-12


def test_zero_severe_upper_bound():
    u = zero_event_upper_bound(100)
    assert abs(u - 0.0295) < 0.001  # 约2.95%，不等于0%


def test_degenerate_bootstrap_flagged():
    bl = [[1.0]] * 10
    cl = [[1.0]] * 10
    r = paired_bootstrap(bl, cl, n_resamples=500, seed=1)
    assert r["degenerate"] is True


def test_mcnemar_no_discordant():
    assert exact_mcnemar(0, 0) == 1.0
    assert 0 < exact_mcnemar(3, 1) < 1

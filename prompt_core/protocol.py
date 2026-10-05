"""Freeze explicit task criteria before observing confirmatory outputs."""
import hashlib
import json
import math
from statistics import NormalDist


def protocol_hash(protocol):
    return hashlib.sha256(json.dumps(protocol, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def freeze_protocol(value, family_index=None):
    if not isinstance(value, dict):
        raise ValueError("评价协议必须是对象")
    texts = {}
    for key in ("goal", "usable_criterion", "severe_criterion", "sampling_description"):
        text = str(value.get(key) or "").strip()
        if not text or len(text) > 4000:
            raise ValueError("目标、可用标准、严重问题标准和抽样说明均须填写（每项不超过4000字）")
        texts[key] = text
    n = value.get("planned_groups")
    if type(n) is not int or not 5 <= n <= 100000:
        raise ValueError("预定独立来源数必须为5至100000的整数")
    family_alpha, gain = value.get("alpha", 0.05), value.get("min_gain", 0.05)
    if family_index is None:
        family_index = value.get("family_index", 1)
    if type(family_index) is not int or family_index < 1:
        raise ValueError("正式检验序号必须是正整数")
    if type(family_alpha) not in (float, int) or not math.isfinite(family_alpha) or not 0 < family_alpha <= 0.05:
        raise ValueError("累计显著性预算必须在0至0.05之间")
    if type(gain) not in (float, int) or not math.isfinite(gain) or not 0 < gain <= 1:
        raise ValueError("最小提升必须在0至1之间")
    if value.get("independence_declared") is not True:
        raise ValueError("须确认这些来源独立、候选已固定且未看过考试输出")
    return dict(texts, version=2, planned_groups=n, alpha=float(family_alpha),
                family_index=family_index,
                alpha_threshold=math.ldexp(float(family_alpha), -family_index),
                multiplicity="geometric_alpha_spending_per_prompt",
                min_gain=float(gain),
                independence_declared=True, primary_metric="direct_usable_rate",
                statistical_unit="source_group", stopping_rule="fixed_sample",
                unknown_policy="worst_case_sensitivity", test="exact_mcnemar_two_sided")


def estimate_sample_size(gain, discordance, alpha=0.05, power=0.8):
    """Normal approximation for planning only, never a proof of representativeness."""
    if not 0 < gain <= discordance <= 1 or not 0 < alpha <= 0.1 or not 0.5 < power < 1:
        raise ValueError("需满足0<预期提升≤两版不同结果比例≤1，功效在0.5至1之间")
    normal = NormalDist()
    n = math.ceil(((normal.inv_cdf(1-alpha/2) + normal.inv_cdf(power)) ** 2
                   * discordance) / gain ** 2)
    return {"suggested_groups": max(5, n), "method": "normal_approximation",
            "assumed_gain": gain, "assumed_discordance": discordance, "power": power,
            "warning": "近似规划值，需用试点估计不同结果比例；不保证罕见风险检测或来源代表性。"}

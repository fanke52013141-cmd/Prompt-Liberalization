"""模型调用供应商层。

- MockProvider：确定性离线模拟供应商。按请求指纹与输入内容确定输出，
  支持注入 429/401/超时/截断/坏JSON 等故障（对应夹具 FX09），用于本地验证全流程。
- OpenAICompatProvider：OpenAI 兼容 /chat/completions 真实接口（智谱/DeepSeek/OpenAI 等），
  密钥只保存在本地 settings，绝不进入日志与导出（TC051）。

模拟质量模型：生成器按提示词特征（分点/结论/具体定位/可执行建议）与逐条确定性
噪声决定输出质量；评价器按输出文本特征打分。提示词改进 -> 输出改变 -> 测得提升，
构成自洽的离线闭环。所有模拟数据均为演示性质（PRD：示例明确为演示数据）。
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request


class ProviderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 http_status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after = retry_after


class CallResult:
    def __init__(self, text: str, finish: str, usage: dict, raw: dict | None = None):
        self.text = text
        self.finish = finish          # stop / length(截断) / error
        self.usage = usage            # {"in": int, "out": int}
        self.raw = raw or {}


def _fingerprint_seed(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


# ---------------------------------------------------------------- 模拟质量模型

_FEATURE_KEYS = {
    "structure": ["分点", "要点", "结构", "逐条"],
    "conclusion": ["结论", "判断", "依据"],
    "specific": ["具体", "定位"],
    "actionable": ["可执行", "步骤", "修改动作"],
}


def prompt_quality(prompt_text: str) -> float:
    """按提示词文本特征计算基础质量（0~1）。"""
    hits = sum(1 for keys in _FEATURE_KEYS.values() if any(k in prompt_text for k in keys))
    return round(0.40 + 0.11 * hits, 4)


def _item_noise(item_key: str) -> float:
    return ((_fingerprint_seed(item_key) % 1000) / 1000.0 - 0.5) * 0.30


def _render_output(q: float, task_label: str) -> str:
    parts = [f"点评（{task_label}）："]
    if q < 0.30:
        parts.append("判断错误，学员做法完全不对，重新做一遍。")
        return "".join(parts)
    parts.append("学员的回答有一定基础。")
    if q >= 0.72:
        parts.append("结论：判断正确，依据是学员对核心概念的处理与参考一致。")
    if q >= 0.58:
        parts.append("定位：具体指出第2步出现概念混淆，导致后续推导偏离。")
    if q >= 0.55:
        parts.append("建议：可执行建议如下——1) 先复核第2步的定义；2) 再按正确定义重写该步；3) 最后对照答案核对。")
    elif q >= 0.45:
        parts.append("建议：建议加强相关练习。")
    else:
        parts.append("整体还不错，继续加油。")
    return "".join(parts)


_SEVERE_MARKER = "判断错误"


def judge_score_output(output_text: str, dimensions: list[str]) -> dict:
    """模拟评价器：按输出文本特征对各维度打 0-3 分。

    返回 {"scores": {dim: int}, "abstain": false}；无法解析输出时 abstain=true。
    """
    scores: dict[str, int] = {}
    for dim in dimensions:
        if dim.startswith("判断") or "正确" in dim:
            if _SEVERE_MARKER in output_text:
                scores[dim] = 0
            elif "判断正确" in output_text:
                scores[dim] = 3
            else:
                scores[dim] = 1
        elif "定位" in dim or "具体" in dim:
            if "定位：具体" in output_text:
                scores[dim] = 3
            elif "定位：" in output_text:
                scores[dim] = 2
            else:
                scores[dim] = 0
        elif "可执行" in dim or "建议" in dim or "步骤" in dim:
            if "可执行建议如下" in output_text:
                scores[dim] = 3
            elif "建议：" in output_text:
                scores[dim] = 1
            else:
                scores[dim] = 0
        elif "逻辑" in dim or "覆盖" in dim or "完整" in dim or "易理解" in dim or "吸引" in dim \
                or "差异" in dim or "风格" in dim or "忠实" in dim or "友好" in dim or "展开" in dim \
                or "论据" in dim or "获得" in dim:
            scores[dim] = 3 if ("依据" in output_text or "定位" in output_text) else 2
        else:
            scores[dim] = 2
    return {"scores": scores, "abstain": False}


def is_usable(scored: dict) -> bool:
    """人工主指标的离线替代：平均分 >= 2.0 视为可直接使用。"""
    vals = list(scored.get("scores", {}).values())
    if not vals:
        return False
    return (sum(vals) / len(vals)) >= 2.0


def has_severe_error(output_text: str, scored: dict) -> bool:
    """严重错误＝虚构/纠正为错误结论（severity_examples），由输出标记识别。"""
    return _SEVERE_MARKER in output_text


# ---------------------------------------------------------------- Providers

class MockProvider:
    name = "mock"

    def __init__(self, config: dict):
        self.config = config

    def complete(self, role: str, model: str, messages: list[dict], params: dict,
                 fingerprint: str) -> CallResult:
        inject = (params or {}).get("mock_inject") or (self.config or {}).get("mock_inject")
        if inject == "http_429":
            raise ProviderError("RATE_LIMITED", "模拟供应商返回429限流", retryable=True,
                                http_status=429, retry_after=1)
        if inject == "http_401":
            raise ProviderError("AUTH_FAILED", "模拟供应商返回401：请检查连接密钥配置",
                                http_status=401)
        if inject == "timeout":
            raise ProviderError("TIMEOUT", "模拟供应商请求超时", retryable=True)

        flat = "\n".join(m.get("content", "") for m in messages)
        task_label = "演示任务"
        m = re.search(r"任务类型[：:]\s*(\S+)", flat)
        if m:
            task_label = m.group(1)
        item_key = ""
        mi = re.search(r"\[案例\s*(\S+?)\]", flat)
        if mi:
            item_key = mi.group(1)

        if role == "evaluation":
            # 问题核查分支：<problem_check> 标记时按输出特征逐项判定专家问题状态
            if "<problem_check>" in flat:
                ids = re.findall(r"id=([A-Za-z0-9_\-]+)", flat)
                text_payload = re.search(r"<output>([\s\S]*?)</output>", flat)
                output_text = text_payload.group(1) if text_payload else ""
                if _SEVERE_MARKER in output_text:
                    status = "unresolved"
                elif "定位：具体" in output_text:
                    status = "resolved"
                elif "定位：" in output_text:
                    status = "partial"
                else:
                    status = "unknown"
                body = json.dumps({"problems": [{"id": i, "status": status,
                                                 "note": "模拟判定：按输出特征（演示）"}
                                                for i in ids]},
                                  ensure_ascii=False)
                usage = {"in": 150 + len(flat) // 2, "out": 40 + len(body) // 2}
                return CallResult(body, "stop", usage)
            dims = re.findall(r"维度[：:]\s*([^\n]+)", flat)
            dims = [d.strip() for d in dims if d.strip()]
            text_payload = re.search(r"<output>([\s\S]*?)</output>", flat)
            output_text = text_payload.group(1) if text_payload else flat[-400:]
            scored = judge_score_output(output_text, dims or ["判断正确"])
            body = json.dumps({"scores": scored["scores"], "abstain": scored["abstain"],
                               "reason": "模拟评价器按输出特征打分（演示）"},
                              ensure_ascii=False)
            if inject == "bad_json":
                body = "这不是JSON输出{"
            if inject == "truncation":
                body = body[: max(5, len(body) // 2)]
                return CallResult(body, "length",
                                  {"in": 150 + len(flat) // 2, "out": 60})
            usage = {"in": 150 + len(flat) // 2, "out": 60 + len(body) // 2}
            return CallResult(body, "stop", usage)

        # generation / optimizer
        q = prompt_quality(flat)
        if role == "optimizer":
            # 反思模型：基于完整证据返回完整新正文（演示版为确定性策略）。
            # 若当前正文已包含分层建议（先给结论…），认为证据已被覆盖，原样返回并说明依据不足。
            body_m = re.search(r"<current_prompt>\n?([\s\S]*?)\n?</current_prompt>", flat)
            current_body = body_m.group(1) if body_m else ""
            if inject == "optimizer_bad_json":
                return CallResult("这不是JSON输出{改写失败演示", "stop",
                                  {"in": 120 + len(flat) // 2, "out": 30})
            if "先给结论" in current_body:
                body = json.dumps(
                    {"hypothesis": "证据不足：当前正文已包含结论-定位-可执行建议的分层要求，"
                                   "没有新的失败模式支持进一步修改；归因应视为待验证假设。",
                     "new_body": current_body,
                     "change_summary": "无修改（无新增依据）"},
                    ensure_ascii=False)
            else:
                suggestion = ("输出要求：先给结论，明确判断对错并说明依据；再具体定位出错的步骤；"
                              "最后给出可执行建议：按以下步骤修改：1) 复核定义；2) 重写该步；"
                              "3) 对照核对。")
                body = json.dumps(
                    {"hypothesis": "失败案例集中在归因含混与定位缺失：在正文尾部增加"
                                   "“结论-定位-可执行建议”三层输出要求，预计减少含混点评。",
                     "new_body": current_body.rstrip() + "\n\n" + suggestion,
                     "change_summary": "追加输出结构要求：结论先行、具体定位、可执行建议三步"},
                    ensure_ascii=False)
            usage = {"in": 120 + len(flat) // 2, "out": 90 + len(body) // 2}
            return CallResult(body, "stop", usage)

        text = _render_output(min(0.98, max(0.05, q + _item_noise(item_key or fingerprint))), task_label)
        if inject == "truncation":
            text = text[: max(4, len(text) // 3)]
            return CallResult(text, "length", {"in": 150 + len(flat) // 2, "out": 40})
        usage = {"in": 150 + len(flat) // 2, "out": 80 + len(text) // 2}
        return CallResult(text, "stop", usage)


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, config: dict):
        self.base_url = (config.get("base_url") or "").rstrip("/")
        self.api_key = config.get("api_key") or ""
        if not self.base_url:
            raise ProviderError("CONFIG_INVALID", "OpenAI兼容连接缺少 base_url")

    def complete(self, role: str, model: str, messages: list[dict], params: dict,
                 fingerprint: str) -> CallResult:
        payload = {"model": model, "messages": messages,
                   **{k: v for k, v in (params or {}).items()
                      if k in ("temperature", "top_p", "max_tokens", "seed")}}
        # 支持参数校验：连接声明不支持的参数必须报配置错误，不能静默丢弃（TC029）
        unsupported = (self.config or {}).get("unsupported_params") or []
        bad = [k for k in payload if k in unsupported]
        if bad:
            raise ProviderError("PARAM_UNSUPPORTED",
                                f"连接 {self.base_url} 不支持参数：{', '.join(bad)}，请修正模型配置")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            ra = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else None
            if e.code == 429:
                raise ProviderError("RATE_LIMITED", "供应商返回429限流，将按Retry-After退避重试",
                                    retryable=True, http_status=429, retry_after=ra)
            if e.code in (401, 403):
                raise ProviderError("AUTH_FAILED", "供应商返回401/403：请检查API密钥与权限",
                                    http_status=e.code)
            raise ProviderError("PROVIDER_HTTP", f"供应商HTTP错误 {e.code}", retryable=e.code >= 500,
                                http_status=e.code)
        except urllib.error.URLError as e:
            raise ProviderError("NETWORK", f"网络错误：{e.reason}", retryable=True)
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {}) or {}
        return CallResult(msg.get("content") or "", choice.get("finish_reason") or "stop",
                          {"in": usage.get("prompt_tokens", 0),
                           "out": usage.get("completion_tokens", 0)}, raw=data)


def get_provider(connection: dict):
    kind = connection.get("provider")
    if kind == "mock":
        return MockProvider(connection)
    if kind == "openai_compat":
        return OpenAICompatProvider(connection)
    raise ProviderError("CONFIG_INVALID", f"未知供应商类型：{kind}")

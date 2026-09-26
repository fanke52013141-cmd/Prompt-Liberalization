"""引擎：请求渲染、计量调用、生成/评价原语与反思式优化器（D06/D07）。

- 渲染只使用 runtime_input 白名单字段；evaluation_only 永不出站（BR01/TC009）
- 每次模型调用都经过账本预留->调用->结算（BR09），重试所有权统一在调用适配器
- 优化器只做"从失败证据提出改写片段->候选评分->择优"，不实现第二个搜索引擎；
  接口形态与 GEPA 对齐（候选集/反思反馈/预算回调），可在 M0 后替换为 GEPA 适配器
"""
from __future__ import annotations

import json
import time

from .core import BizError, canonical_hash, new_id, now_iso
from .db import DB, get_db
from .ledger import BudgetState, Ledger
from .providers import CallResult, ProviderError, get_provider

_MAX_ATTEMPTS = 2


def _get_provider(connection: dict):
    return get_provider(connection)


def get_connection(model_config: dict) -> dict:
    """从 settings 解析连接配置；密钥只在此层使用，不进日志/响应。"""
    db = get_db()
    row = db.one("SELECT value_json FROM settings WHERE key='connections'")
    conns = json.loads(row["value_json"]) if row else []
    cid = model_config.get("connection_id")
    for c in conns:
        if c["id"] == cid:
            return c
    raise BizError("CONNECTION_NOT_FOUND", f"模型连接不存在或已删除：{cid}")


def render_messages(prompt_version: dict, item_runtime: dict, task_label: str) -> list[dict]:
    """按白名单渲染请求消息。额外字段与 evaluation_only 不会出现在任何消息里。"""
    variables = prompt_version.get("variables", [])
    runtime = item_runtime.get("runtime_input", {})
    missing = [v for v in variables if v not in runtime]
    if missing:
        raise BizError("VARIABLE_MISSING", f"模板变量缺失：{missing}（TC030）")
    body = prompt_version["body"]
    for v in variables:
        body = body.replace("{{" + v + "}}", str(runtime.get(v, "")))
    lines = [f"任务类型：{task_label}", "[案例 " + item_runtime.get("case_id", "") + "]"]
    for k, v in runtime.items():
        if k in variables:  # 白名单：只传提示词声明的变量（BR01）
            lines.append(f"{k}：{v}")
    return [{"role": "system", "content": body}, {"role": "user", "content": "\n".join(lines)}]


def call_model(db: DB, role: str, model_config: dict, messages: list[dict], params: dict,
               run_id: str, logical_id: str, phase: str,
               budget: BudgetState, ledger: Ledger) -> CallResult:
    """统一计量入口：生成/评价/优化全部角色都必须经过（BR09）。"""
    connection = get_connection(model_config)
    model = model_config.get("model") or connection.get("model") or "default"
    flat = "\n".join(m.get("content", "") for m in messages)
    fingerprint = canonical_hash({"role": role, "model": model, "messages": messages,
                                  "params": params, "engine": "prompt-lab-v0.1"})
    est = len(flat) // 2 + 200
    provider = _get_provider(connection)
    last_err: ProviderError | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        attempt_id = ledger.reserve(run_id, logical_id, role, model, phase, est, budget, fingerprint)
        try:
            result = provider.complete(role, model, messages, params, fingerprint)
        except ProviderError as e:
            last_err = e
            if e.retryable and _attempt < _MAX_ATTEMPTS - 1:
                ledger.mark_failed(attempt_id, budget)
                time.sleep(min(e.retry_after or 0.05, 0.2))  # 尊重 Retry-After（演示取短）
                continue
            if e.code == "RATE_LIMITED":
                # 供应商已接收但未返回：保守计为 sent_unknown 保留潜在费用（TC040演示口径）
                ledger.mark_sent_unknown(attempt_id, budget)
            else:
                ledger.mark_failed(attempt_id, budget)
            raise
        ledger.settle(attempt_id, budget, result.usage.get("in", 0), result.usage.get("out", 0))
        return result
    raise last_err or ProviderError("UNKNOWN", "调用失败")


def generate_once(db: DB, project: dict, prompt_version: dict, item: dict,
                  model_config: dict, run_id: str, phase: str,
                  budget: BudgetState, ledger: Ledger, logical_id: str | None = None) -> dict:
    """对一个案例执行一次生成并落库。失败/截断保存明确状态，不伪造输出（TC030）。"""
    contract = project["contract"]
    from .core import TASK_TEMPLATES
    label = TASK_TEMPLATES.get(contract["task_type"], {}).get("label", contract["task_type"])
    messages = render_messages(prompt_version, item, label)
    params = dict(prompt_version.get("params") or {})
    params.update(model_config.get("params") or {})
    logical_id = logical_id or new_id("lrq")
    request_hash = canonical_hash({"messages": messages, "params": params})
    try:
        result = call_model(db, "generation", model_config, messages, params, run_id, logical_id,
                            phase, budget, ledger)
    except ProviderError as e:
        oid = new_id("out")
        db.execute(
            "INSERT INTO outputs(id,project_id,item_id,prompt_version_id,run_id,role,text,status,"
            "usage_json,request_hash,created_at) VALUES(?,?,?,?,?,'generation','', 'failed', ?, ?, ?)",
            (oid, project["id"], item["id"], prompt_version["id"], run_id,
             json.dumps({"error": e.code, "message": e.message}, ensure_ascii=False),
             request_hash, now_iso()))
        return {"id": oid, "status": "failed", "error": e.code}
    status = "ok" if result.finish == "stop" else "incomplete"  # 截断明确标记（TC030）
    oid = new_id("out")
    db.execute(
        "INSERT INTO outputs(id,project_id,item_id,prompt_version_id,run_id,role,text,status,"
        "usage_json,request_hash,created_at) VALUES(?,?,?,?,?,'generation',?,?,?,?,?)",
        (oid, project["id"], item["id"], prompt_version["id"], run_id, result.text, status,
         json.dumps(result.usage), request_hash, now_iso()))
    return {"id": oid, "status": status, "text": result.text}


def evaluate_once(output_text: str, rubric_id: str, model_config: dict, run_id: str = "trial",
                  phase: str = "search") -> dict:
    """评价一个输出：返回 {"scores", "abstain", "error"?}。评价失败不静默当0分。"""
    db = get_db()
    rubric = db.one("SELECT * FROM rubrics WHERE id=?", (rubric_id,))
    if rubric is None:
        raise BizError("NOT_FOUND", "评价标准不存在", status=404)
    dims = [d["name"] for d in json.loads(rubric["schema_json"]).get("dimensions", [])]
    dim_lines = "\n".join(f"维度：{d}" for d in dims)
    messages = [
        {"role": "system", "content":
            "你是评价器。按标准对输出逐维度打0-3分，只返回JSON：{\"scores\":{维度:分数},\"abstain\":false}。\n"
            + dim_lines},
        {"role": "user", "content": f"<output>\n{output_text}\n</output>"},
    ]
    budget = BudgetState({"mode": "token", "total_limit": 10 ** 9, "search_limit": 10 ** 9,
                          "acceptance_limit": 10 ** 9})
    ledger = Ledger(db)
    try:
        result = call_model(db, "evaluation", model_config, messages, {}, run_id,
                            new_id("lrq"), phase, budget, ledger)
    except ProviderError as e:
        return {"abstain": True, "error": e.code}
    try:
        data = json.loads(result.text)
        scores = data.get("scores", {})
        out = {"scores": {d: scores[d] for d in dims if isinstance(scores.get(d), int)},
               "abstain": bool(data.get("abstain"))}
        if result.finish != "stop":
            out = {"abstain": True, "error": "truncated"}
        return out
    except Exception:
        return {"abstain": True, "error": "bad_json"}  # 评价坏JSON：unknown，不删分母（TC030）


# ---------------------------------------------------------------- 优化器

_OPT_FRAGMENT = ("输出要求：先给结论，明确判断对错并说明依据；再具体定位出错的步骤；"
                 "最后给出可执行建议：按以下步骤修改：1) 复核定义；2) 重写该步；3) 对照核对。")


def propose_fragment(db: DB, model_config: dict, failures: list[dict], run_id: str,
                     budget: BudgetState, ledger: Ledger) -> str:
    """反思：由失败证据提出改写片段（GEPA 式 reflect 接口的本地实现）。"""
    if not failures:
        return _OPT_FRAGMENT
    flat = "\n".join(f"案例 {f.get('case')}: 低分维度 {f.get('dims')}" for f in failures[:5])
    messages = [{"role": "user", "content":
                 "以下是当前提示词在开发集上的失败摘要，请提出一段可直接追加的输出要求片段。\n" + flat}]
    try:
        result = call_model(db, "optimizer", model_config, messages, {}, run_id,
                            new_id("lrq"), "search", budget, ledger)
        data = json.loads(result.text)
        frag = data.get("fragment_suggestion")
        if frag:
            return str(frag)
    except (ProviderError, ValueError, KeyError):
        pass
    return _OPT_FRAGMENT


def score_prompt(db: DB, project: dict, prompt_version: dict, item_ids: list[str],
                 rubric_id: str, eval_model: dict, run_id: str, phase: str,
                 budget: BudgetState, ledger: Ledger) -> dict:
    """在固定样本上生成+评价，返回 {score, usable_rate, n, n_scored, severe, failures}。"""
    data_svc = None
    from .domain import DataService
    data_svc = DataService(db)
    scores, usable, n, n_scored, severe, failures = [], 0, 0, 0, 0, []
    for iid in item_ids:
        item = data_svc.get_item_runtime(project["id"], iid)
        gen = generate_once(db, project, prompt_version, item, eval_model.get("generation")
                            if isinstance(eval_model, dict) and "generation" in eval_model
                            else eval_model, run_id, phase, budget, ledger)
        n += 1
        if gen["status"] != "ok":
            continue  # 失败/截断计入分母但不得分：unknown 不删分母（TC030）
        out_row = db.one("SELECT text FROM outputs WHERE id=?", (gen["id"],))
        ev_model = eval_model.get("evaluation") if isinstance(eval_model, dict) and \
            "evaluation" in eval_model else eval_model
        scored = evaluate_once(out_row["text"], rubric_id, ev_model, run_id, phase)
        if scored.get("abstain"):
            continue
        n_scored += 1
        vals = list(scored["scores"].values())
        s = sum(vals) / len(vals) if vals else 0.0
        scores.append(s)
        from .providers import has_severe_error, is_usable
        if is_usable(scored):
            usable += 1
        if has_severe_error(out_row["text"], scored):
            severe += 1
            failures.append({"case": item["case_id"], "dims": [k for k, v in
                             scored["scores"].items() if v <= 1]})
    return {"score": (sum(scores) / len(scores)) if scores else 0.0,
            "usable_rate": usable / n if n else 0.0, "n": n, "n_scored": n_scored,
            "severe": severe, "failures": failures}

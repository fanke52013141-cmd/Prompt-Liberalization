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


# ---------------------------------------------------------------- 优化器（优化1.0 §8）

def build_optimizer_messages(current_pv: dict, project: dict, rubric: dict,
                             evidence: dict, history: list[dict],
                             length_limit: int) -> list[dict]:
    """优化器的完整输入（§8.2）：不得只发送等级、案例编号或低分维度。

    包含：当前提示词与可修改范围（冻结组件单列）、任务目标与评价标准、
    实际输入与实际输出、专家问题/标签/备注/证据、原来正确的行为与代表案例、
    历史修改与测评结果。
    注意 BR01：runtime 白名单之外的字段与 evaluation_only 数据（如参考答案）永不进入。
    """
    contract = project["contract"]
    runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
    frozen = current_pv.get("frozen_segments", [])
    frozen_names = "、".join(s.get("name", "") for s in frozen) or "（无）"
    dims = rubric.get("dimensions") or []
    dim_lines = "\n".join(f"- {d.get('name')}（0锚点：{d.get('anchors', {}).get('0', '')}）"
                          for d in dims) or "（未提供维度锚点）"
    sev = rubric.get("severity_examples") or []

    def fmt_input(ri: dict) -> str:
        return "\n".join(f"  {k}：{v}" for k, v in (ri or {}).items() if k in runtime_names) or "  （无）"

    fail_blocks = []
    for f in evidence.get("failures", []):
        fb_lines = []
        for fb in f.get("feedback", []):
            line = (f"  - 专家问题（{fb.get('severity', 'normal')}，状态{fb.get('status', '')}"
                    f"，标签{'、'.join(fb.get('tags') or []) or '无'}）：{fb.get('problem')}")
            if fb.get("quote"):
                line += f"；专家原话：{fb['quote']}"
            if fb.get("expected"):
                line += f"；期望：{fb['expected']}"
            if fb.get("remark"):
                line += f"；备注：{fb['remark']}"
            fb_lines.append(line)
        fail_blocks.append(
            f"【失败案例 {f.get('case_id')}】维度得分：{json.dumps(f.get('dims') or {}, ensure_ascii=False)}\n"
            f"输入：\n{fmt_input(f.get('runtime_input'))}\n"
            f"当前输出：\n{f.get('output_text', '')}\n"
            + ("专家意见：\n" + "\n".join(fb_lines) if fb_lines else "专家意见：（该案例无登记的专家意见）"))
    correct_blocks = [
        f"【正确案例 {c.get('case_id')}】维度得分：{json.dumps(c.get('dims') or {}, ensure_ascii=False)}\n"
        f"输入：\n{fmt_input(c.get('runtime_input'))}\n当前输出：\n{c.get('output_text', '')}"
        for c in evidence.get("correct", [])]
    hist_lines = [f"- 第{h.get('round')}轮（{h.get('decision')}）：假设={h.get('hypothesis')}；"
                  f"结果={h.get('result')}；依据={h.get('rationale')}" for h in history]
    goal = contract.get("goal") or "（用户未填写优化目标）"
    sys_content = (
        "你是提示词优化器。根据给定的失败证据修改提示词正文，并给出可验证的修改假设。\n"
        "硬性约束：\n"
        f"1. 冻结组件不可修改、不可删除、不可在正文中复写：{frozen_names}\n"
        f"2. 模板变量只能使用白名单：{'、'.join(runtime_names)}；"
        "不得引入白名单外变量或评价专用字段\n"
        f"3. 新正文不超过{length_limit}字（当前{len(current_pv.get('body', ''))}字）："
        "限制无依据追加与长度膨胀\n"
        '4. 只返回JSON：{"hypothesis":"修改假设：预计解决什么、可能原因",'
        '"new_body":"完整新正文","change_summary":"实际修改点摘要"}\n'
        "5. 证据不足以支持修改时，new_body 原样返回并在 hypothesis 中说明。\n"
        "6. 归因是待验证假设：资料不足、评价错误、运行故障或模型能力限制，"
        "不应被强行解释为提示词缺陷。")
    user_content = (
        f"任务：{contract.get('label', '')}\n优化目标：{goal}\n"
        f"评价标准维度（含0分锚点）：\n{dim_lines}\n"
        + (f"严重问题示例：{'、'.join(sev)}\n" if sev else "")
        + "\n<current_prompt>\n" + current_pv.get("body", "") + "\n</current_prompt>\n\n"
        + ("== 失败证据（本轮为什么改）==\n" + "\n\n".join(fail_blocks)
           if fail_blocks else "== 失败证据 ==\n（本轮无失败案例证据）")
        + "\n\n== 必须保持的正确行为 ==\n"
        + ("\n\n".join(correct_blocks) if correct_blocks else "（暂无正确案例记录）")
        + "\n\n== 历史修改与测评结果 ==\n"
        + ("\n".join(hist_lines) if hist_lines else "（第一轮，无历史）"))
    return [{"role": "system", "content": sys_content},
            {"role": "user", "content": user_content}]


def propose_revision(db: DB, model_config: dict, current_pv: dict, project: dict,
                     rubric: dict, evidence: dict, history: list[dict],
                     run_id: str, budget: BudgetState, ledger: Ledger,
                     length_limit: int = 4000) -> dict:
    """由完整证据提出改写（GEPA 式 reflect 的本地实现）。

    改写失败（调用失败/坏JSON/空正文/超长）明确报告，绝不静默追加固定业务文本
    充当成功结果（§8.6）；返回 {"ok": bool, ...}。
    """
    messages = build_optimizer_messages(current_pv, project, rubric, evidence, history, length_limit)
    try:
        result = call_model(db, "optimizer", model_config, messages, {}, run_id,
                            new_id("lrq"), "search", budget, ledger)
    except ProviderError as e:
        return {"ok": False, "reason": f"改写失败：优化模型调用失败（{e.code}），本轮未产生候选"}
    if result.finish != "stop":
        return {"ok": False, "reason": "改写失败：优化模型输出被截断，本轮未产生候选"}
    try:
        data = json.loads(result.text)
        new_body = str(data["new_body"])
        hypothesis = str(data.get("hypothesis", ""))
        change_summary = str(data.get("change_summary", ""))
    except (ValueError, KeyError, TypeError):
        return {"ok": False,
                "reason": "改写失败：优化模型未返回约定的JSON结构（hypothesis/new_body），"
                          "本轮未产生候选；未使用任何兜底文本"}
    if not new_body.strip():
        return {"ok": False, "reason": "改写失败：优化模型返回空正文，本轮未产生候选"}
    if len(new_body) > length_limit:
        return {"ok": False, "reason": f"改写失败：新正文 {len(new_body)} 字超过长度上限 "
                                       f"{length_limit} 字（控制无依据膨胀，§8.6），本轮未产生候选"}
    return {"ok": True, "hypothesis": hypothesis, "new_body": new_body,
            "change_summary": change_summary, "length": len(new_body)}


def check_problems(db: DB, problems: list[dict], output_text: str, model_config: dict,
                   run_id: str, phase: str, budget: BudgetState, ledger: Ledger) -> dict:
    """对一份输出逐项核查专家问题是否仍存在：resolved/partial/unresolved/unknown。

    需要语义理解，因此使用自动判定并接受专家核对（§6.6）；失败记 unknown，不静默当已解决。
    """
    if not problems:
        return {"statuses": {}, "abstain": False}
    plines = "\n".join(f"- id={p['id']}：{p['problem']}"
                       + (f"（期望：{p.get('expected')}）" if p.get("expected") else "")
                       for p in problems)
    messages = [
        {"role": "system", "content":
            "你是问题核查判定者。对照以下专家问题逐项检查输出是否仍存在该问题。\n"
            "只返回JSON：{\"problems\":[{\"id\":\"...\",\"status\":\"resolved|partial|"
            "unresolved|unknown\",\"note\":\"判定依据\"}]}。\n" + plines},
        {"role": "user", "content": f"<problem_check>\n<output>\n{output_text}\n</output>"},
    ]
    try:
        result = call_model(db, "evaluation", model_config, messages, {}, run_id,
                            new_id("lrq"), phase, budget, ledger)
    except ProviderError as e:
        return {"statuses": {p["id"]: "unknown" for p in problems},
                "abstain": True, "error": e.code}
    try:
        data = json.loads(result.text)
        valid = ("resolved", "partial", "unresolved", "unknown")
        out = {}
        for entry in data.get("problems", []):
            pid = str(entry.get("id", ""))
            if pid in {p["id"] for p in problems} and entry.get("status") in valid:
                out[pid] = entry["status"]
        for p in problems:  # 缺项记 unknown：不删除分母（TC030 口径）
            out.setdefault(p["id"], "unknown")
        return {"statuses": out, "abstain": False}
    except Exception:
        return {"statuses": {p["id"]: "unknown" for p in problems},
                "abstain": True, "error": "bad_json"}


def score_prompt(db: DB, project: dict, prompt_version: dict, item_ids: list[str],
                 rubric_id: str, eval_model: dict, run_id: str, phase: str,
                 budget: BudgetState, ledger: Ledger) -> dict:
    """在固定样本上生成+评价，返回汇总与逐案例明细（供回退检查与证据追溯）。"""
    from .domain import DataService
    data_svc = DataService(db)
    scores, usable, n, n_scored, severe, failures = [], 0, 0, 0, 0, []
    items_detail = []
    for iid in item_ids:
        item = data_svc.get_item_runtime(project["id"], iid)
        gen = generate_once(db, project, prompt_version, item, eval_model.get("generation")
                            if isinstance(eval_model, dict) and "generation" in eval_model
                            else eval_model, run_id, phase, budget, ledger)
        n += 1
        entry = {"item_id": iid, "case_id": item["case_id"], "output_id": gen["id"],
                 "gen_status": gen["status"], "score": None, "usable": None,
                 "severe": None, "eval_abstain": True, "dims": {}}
        if gen["status"] != "ok":
            items_detail.append(entry)
            continue  # 失败/截断计入分母但不得分：unknown 不删分母（TC030）
        out_row = db.one("SELECT text FROM outputs WHERE id=?", (gen["id"],))
        entry["output_text"] = out_row["text"]
        ev_model = eval_model.get("evaluation") if isinstance(eval_model, dict) and \
            "evaluation" in eval_model else eval_model
        scored = evaluate_once(out_row["text"], rubric_id, ev_model, run_id, phase)
        if scored.get("abstain"):
            items_detail.append(entry)
            continue
        n_scored += 1
        vals = list(scored["scores"].values())
        s = sum(vals) / len(vals) if vals else 0.0
        scores.append(s)
        entry["score"] = s
        entry["dims"] = scored["scores"]
        entry["eval_abstain"] = False
        from .providers import has_severe_error, is_usable
        u = is_usable(scored)
        entry["usable"] = u
        sv = has_severe_error(out_row["text"], scored)
        entry["severe"] = sv
        if u:
            usable += 1
        if sv:
            severe += 1
            failures.append({"case": item["case_id"], "item_id": iid,
                             "dims": [k for k, v in scored["scores"].items() if v <= 1]})
        items_detail.append(entry)
    return {"score": (sum(scores) / len(scores)) if scores else 0.0,
            "usable_rate": usable / n if n else 0.0, "n": n, "n_scored": n_scored,
            "severe": severe, "failures": failures, "items": items_detail,
            "length": len(prompt_version.get("body", ""))}


def decide_keep(prev: dict, cand: dict, min_delta: float) -> dict:
    """保留决定（§8.6）：比较主要目标、底线与代价，不只比较平均分。

    底线：原有正确案例不得回退；严重错误不得增加。归因与依据写入 rationale。
    """
    regressions = cand.get("regressions", 0)
    severe_delta = (cand.get("severe") or 0) - (prev.get("severe") or 0)
    score_gain = (cand.get("score") or 0.0) - (prev.get("score") or 0.0)
    fixed = cand.get("fixed_problems", 0)
    open_before = cand.get("open_problems_before", 0)
    if score_gain > min_delta and regressions == 0 and severe_delta <= 0:
        decision, rationale = "kept", (
            f"平均分 {prev.get('score'):.3f}→{cand.get('score'):.3f}（+{score_gain:.3f}，"
            f"超过阈值{min_delta}）；专家问题解决 {fixed}/{open_before}；"
            f"原有正确案例回退 {regressions} 条；严重错误 {prev.get('severe')}→{cand.get('severe')}；"
            f"长度 {prev.get('length')}→{cand.get('length')} 字：超过阈值且无回退，保留为新最优")
    elif regressions > 0:
        decision, rationale = "discarded", (
            f"平均分 {prev.get('score'):.3f}→{cand.get('score'):.3f}，但原有正确案例回退 "
            f"{regressions} 条：底线被破坏，淘汰（§8.6 防止越改越差）")
    elif severe_delta > 0:
        decision, rationale = "discarded", (
            f"严重错误 {prev.get('severe')}→{cand.get('severe')}：严重问题不能由其他高分抵消，淘汰")
    elif score_gain <= min_delta:
        decision, rationale = "discarded", (
            f"平均分 {prev.get('score'):.3f}→{cand.get('score'):.3f}（+{score_gain:.3f}，"
            f"未超过阈值{min_delta}），问题解决 {fixed}/{open_before}：改善不足以抵消变化成本，淘汰")
    else:
        decision, rationale = "discarded", "未通过保留条件，淘汰"
    return {"decision": decision, "rationale": rationale,
            "score_gain": score_gain, "regressions": regressions,
            "severe_delta": severe_delta}

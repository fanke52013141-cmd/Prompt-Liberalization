"""预算与调用账本（D08 / TC032—TC035, TC040, TC057）。

每次请求：logical_request_id + attempt_id。派发前以原子更新预留预计上限；
成功写 actual 并释放差额；供应商已接收但响应丢失标 sent_unknown 保留潜在费用，
不无限重试；价格未知时禁止金额硬预算，只允许明确 token 上限模式。
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import json

from .core import BizError, canonical_hash, new_id, now_iso
from .db import DB


class BudgetState:
    """运行预算状态：spent / reserved 按 phase 累计。"""

    def __init__(self, data: dict):
        self.mode = data.get("mode", "token")            # token / money
        def number(value):
            try:
                n = Decimal(str(value))
                if isinstance(value, bool) or not n.is_finite() or n < 0 or (self.mode == "token" and n != n.to_integral_value()):
                    raise ValueError()
                return n if self.mode == "money" else int(n)
            except (InvalidOperation, ValueError, TypeError):
                raise BizError("BUDGET_INVALID", "预算必须为有限非负数；token额度必须为整数", status=422)
        self.total_limit = number(data.get("total_limit", 0))
        self.search_limit = number(data.get("search_limit", 0))
        self.acceptance_limit = number(data.get("acceptance_limit", 0))
        self.spent = {phase: number((data.get("spent") or {}).get(phase, 0)) for phase in ("search", "acceptance")}
        self.reserved = {phase: number((data.get("reserved") or {}).get(phase, 0)) for phase in ("search", "acceptance")}
        self.prices = data.get("prices") or {}

    def to_json(self) -> dict:
        result = {"mode": self.mode, "total_limit": self.total_limit,
                "search_limit": self.search_limit, "acceptance_limit": self.acceptance_limit,
                "spent": self.spent, "reserved": self.reserved, "prices": self.prices}
        return json.loads(json.dumps(result, default=str))

    def validate(self) -> None:
        if self.mode not in ("token", "money"):
            raise BizError("BUDGET_MODE_INVALID", "预算模式只支持 token 或 money")
        if self.total_limit <= 0:
            raise BizError("BUDGET_INVALID", "必须设置大于0的总额度上限")
        if self.search_limit + self.acceptance_limit > self.total_limit:
            raise BizError("BUDGET_PHASE_CONFLICT",
                           "搜索额度与独立验收预留之和不能超过总额度（验收预留单独保护，TC033）")
        if self.search_limit <= 0:
            raise BizError("BUDGET_INVALID", "必须为搜索阶段设置额度；独立验收额度单独预留")

    def phase_limit(self, phase: str) -> int:
        return self.search_limit if phase == "search" else self.acceptance_limit


def validate_money_budget_needs_prices(budget: dict, models: dict, price_table: dict) -> None:
    """金额硬预算要求所有角色模型都有已核实的单价（TC032）。"""
    if budget.get("mode") != "money":
        return
    missing = []
    for role, mc in (models or {}).items():
        model = (mc or {}).get("model", "")
        if model not in price_table:
            missing.append(f"{role}:{model}")
    if missing:
        raise BizError("PRICE_UNKNOWN",
                       "以下模型没有已核实的价格表，不能承诺金额硬预算；请改用明确的token上限模式："
                       + "、".join(missing))
    currencies = set()
    for mc in models.values():
        price = price_table[mc["model"]]
        try:
            amounts = [Decimal(str(price[key])) for key in ("in_per_1k", "out_per_1k")]
            if any(not n.is_finite() or n < 0 for n in amounts):
                raise ValueError()
            if not isinstance(price["currency"], str) or not price["currency"].strip():
                raise ValueError()
            currencies.add(price["currency"])
        except (KeyError, TypeError, InvalidOperation, ValueError):
            raise BizError("PRICE_INVALID", "每个模型价格须包含非负有限输入/输出单价及币种", status=422)
    if len(currencies) != 1:
        raise BizError("PRICE_CURRENCY_CONFLICT", "同一金额预算不能混用不同币种", status=422)


def token_cost(price, tokens_in, tokens_out):
    return (Decimal(str(price["in_per_1k"])) * tokens_in +
            Decimal(str(price["out_per_1k"])) * tokens_out) / 1000


class Ledger:
    def __init__(self, db: DB):
        self.db = db

    def public_attempts(self, run_id):
        columns = "attempt_id,role,model,phase,status,request_hash,reserved_tokens,actual_in,actual_out,reserved_amount,actual_cost,currency,created_at,resolution_json,response_resolution_json,retry_authorization_json,retry_consumed"
        return [dict(row) for row in self.db.query(f'SELECT {columns} FROM ledger WHERE run_id=? ORDER BY created_at,rowid', (run_id,))]

    def completed_response(self, run_id, logical_id, request_hash, retry_authorization_id=None):
        rows = self.db.query("SELECT status,request_hash,response_json FROM ledger WHERE run_id=? AND logical_id=? "
                             "ORDER BY attempt_index DESC", (run_id, logical_id))
        if any(row["request_hash"] != request_hash for row in rows):
            raise BizError("CALL_IDENTITY_CONFLICT", "调用编号对应的请求内容已变化", status=409)
        for row in rows:
            if row["status"] in ("ok", "usage_unknown") and row["response_json"]:
                return json.loads(row["response_json"])
        if any(row["status"] in ("reserved", "sent_unknown", "usage_unknown", "ok") for row in rows):
            if retry_authorization_id and self.retry_authorization_id(run_id, logical_id) == retry_authorization_id:
                return None
            raise BizError("CALL_RESULT_UNCONFIRMED", "旧调用结果尚未确认，不能自动重复派发；请核对供应商记录", status=409)
        return None

    def retry_authorized(self, run_id, logical_id):
        return bool(self.retry_authorization_id(run_id, logical_id))

    def retry_authorization_id(self, run_id, logical_id):
        row = self.db.one("SELECT attempt_id,status,retry_authorization_json,retry_consumed FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index DESC LIMIT 1", (run_id, logical_id))
        return row["attempt_id"] if row and row["status"] == "sent_unknown" and row["retry_authorization_json"] and not row["retry_consumed"] else None

    @staticmethod
    def _persist_budget(conn, run_id, budget):
        payload = (json_dumps(budget.to_json()), now_iso(), run_id)
        updated = conn.execute("UPDATE runs SET budget_state_json=?,updated_at=? WHERE id=?", payload)
        if not updated.rowcount:
            conn.execute("UPDATE severity_audits SET budget_state_json=?,updated_at=? WHERE id=?", payload)

    @staticmethod
    def _refresh(conn, run_id: str, budget: BudgetState) -> None:
        """Use committed limits and ledger occupancy, never a caller's stale copy."""
        run = conn.execute("SELECT budget_state_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            run = conn.execute("SELECT budget_state_json FROM severity_audits WHERE id=?", (run_id,)).fetchone()
        if run:
            saved = json.loads(run["budget_state_json"] or "{}")
            if saved:
                fresh = BudgetState(saved)
                for key in ("mode", "total_limit", "search_limit", "acceptance_limit", "prices"):
                    setattr(budget, key, getattr(fresh, key))
        if budget.mode == "money":
            budget.spent = {"search": Decimal(0), "acceptance": Decimal(0)}
            budget.reserved = {"search": Decimal(0), "acceptance": Decimal(0)}
            for row in conn.execute("SELECT * FROM ledger WHERE run_id=?", (run_id,)):
                if row["status"] == "ok":
                    if not row["actual_cost"]:
                        raise BizError("LEGACY_MONEY_LEDGER", "旧金额运行缺少价格结算，请新建实验", status=409)
                    budget.spent[row["phase"]] += Decimal(row["actual_cost"])
                elif row["status"] in ("reserved", "sent_unknown", "usage_unknown"):
                    if row["price_json"] == "{}":
                        raise BizError("LEGACY_MONEY_LEDGER", "旧金额运行缺少冻结价格，请新建实验", status=409)
                    budget.reserved[row["phase"]] += Decimal(row["reserved_amount"])
            return
        rows = conn.execute(
            "SELECT phase, SUM(CASE WHEN status='ok' THEN COALESCE(actual_in,0)+COALESCE(actual_out,0) "
            "ELSE 0 END) spent, SUM(CASE WHEN status IN ('reserved','sent_unknown','usage_unknown') "
            "THEN reserved_tokens ELSE 0 END) reserved FROM ledger WHERE run_id=? GROUP BY phase",
            (run_id,)).fetchall()
        budget.spent = {"search": 0, "acceptance": 0}
        budget.reserved = {"search": 0, "acceptance": 0}
        for row in rows:
            budget.spent[row["phase"]] = row["spent"]
            budget.reserved[row["phase"]] = row["reserved"]

    def reserve(self, run_id: str, logical_id: str, role: str, model: str, phase: str,
                est_tokens: int, budget: BudgetState, request_hash: str = "", *,
                estimated_in: int | None = None, estimated_out: int | None = None,
                retry_authorization_id: str | None = None) -> str:
        """原子预留：spent + reserved + est <= phase_limit，否则 BUDGET_EXHAUSTED。"""
        if phase not in ("search", "acceptance"):
            raise BizError("BUDGET_PHASE_INVALID", "调用阶段无效", status=422)
        if isinstance(est_tokens, bool) or not isinstance(est_tokens, int) or est_tokens <= 0:
            raise BizError("BUDGET_RESERVATION_INVALID", "预留token必须为正整数", status=422)
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._refresh(conn, run_id, budget)
            audit = conn.execute('SELECT state FROM severity_audits WHERE id=?',(run_id,)).fetchone()
            if audit and audit[0] in ('cancelled','completed'):
                raise BizError('AUDIT_STATE_INVALID','审计已停止或完成，不再预留新调用',status=409)
            if retry_authorization_id:
                grant = conn.execute("SELECT attempt_id,status,retry_authorization_json,retry_consumed FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index DESC LIMIT 1", (run_id, logical_id)).fetchone()
                if not grant or grant["attempt_id"] != retry_authorization_id or grant["status"] != "sent_unknown" or not grant["retry_authorization_json"] or grant["retry_consumed"]:
                    raise BizError("RETRY_AUTHORIZATION_CONSUMED", "重试授权已使用或请求已变化，请刷新核对", status=409)
            pending = conn.execute("SELECT 1 FROM ledger WHERE run_id=? AND logical_id=? AND status='reserved'",
                                   (run_id, logical_id)).fetchone()
            if pending:
                raise BizError("CALL_ALREADY_IN_FLIGHT", "相同调用正在执行", status=409)
            completed = conn.execute("SELECT 1 FROM ledger WHERE run_id=? AND logical_id=? "
                                     "AND status IN ('ok','usage_unknown') AND response_json<>''",
                                     (run_id, logical_id)).fetchone()
            if completed:
                raise BizError("CALL_ALREADY_COMPLETED", "相同调用已完成，请读取保存结果", status=409)
            price = {}
            amount = est_tokens
            if budget.mode == "money":
                validate_money_budget_needs_prices({"mode": "money"}, {role: {"model": model}}, budget.prices)
                if estimated_in is None or estimated_out is None:
                    raise BizError("MONEY_ESTIMATE_REQUIRED", "金额预留需要分别估算输入和输出", status=422)
                if (any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (estimated_in, estimated_out))
                        or estimated_in + estimated_out != est_tokens):
                    raise BizError("MONEY_ESTIMATE_INVALID", "输入输出估算必须为非负整数且与token预留一致", status=422)
                price = budget.prices[model]
                amount = token_cost(price, estimated_in, estimated_out)
            phase_spent = budget.spent.get(phase, 0)
            phase_reserved = budget.reserved.get(phase, 0)
            if phase_spent + phase_reserved + amount > budget.phase_limit(phase):
                raise BizError("BUDGET_EXHAUSTED",
                               f"阶段[{phase}]预算不足：已用{phase_spent}+在途{phase_reserved}"
                               f"+本次预计{amount}超过上限{budget.phase_limit(phase)}；"
                               "独立验收预留不会被搜索占用（TC033）")
            if (budget.spent.get("search", 0) + budget.spent.get("acceptance", 0)
                    + budget.reserved.get("search", 0) + budget.reserved.get("acceptance", 0)
                    + amount > budget.total_limit):
                raise BizError("BUDGET_EXHAUSTED", "总预算不足，运行将暂停")
            budget.reserved[phase] = phase_reserved + amount
            latest = conn.execute("SELECT attempt_id,status,retry_authorization_json,retry_consumed FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index DESC LIMIT 1", (run_id, logical_id)).fetchone()
            if latest and latest["status"] == "sent_unknown" and latest["retry_authorization_json"] and not latest["retry_consumed"]:
                conn.execute("UPDATE ledger SET retry_consumed=1 WHERE attempt_id=?", (latest["attempt_id"],))
            attempt_id = new_id("atm")
            conn.execute(
                "INSERT INTO ledger(id, run_id, logical_id, attempt_id, attempt_index, role, model,"
                " phase, reserved_tokens, status, request_hash, created_at,reserved_amount,price_json)"
                " VALUES(?,?,?,?,?,?,?,?,?, 'reserved', ?, ?,?,?)",
                (attempt_id, run_id, logical_id, attempt_id,
                 self._next_attempt_index(conn, run_id, logical_id), role, model, phase,
                 est_tokens, request_hash, now_iso(), str(amount), json_dumps(price)))
            self._persist_budget(conn, run_id, budget)
            return attempt_id

    @staticmethod
    def _next_attempt_index(conn, run_id: str, logical_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_index),0)+1 AS n FROM ledger WHERE run_id=? AND logical_id=?",
            (run_id, logical_id)).fetchone()
        return row["n"]

    def settle(self, attempt_id: str, budget: BudgetState, actual_in: int, actual_out: int,
               currency: str = "", actual_cost: str = "", response: dict | None = None,
               reconciliation: dict | None = None) -> None:
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (actual_in, actual_out)):
            raise BizError("LEDGER_USAGE_INVALID", "实际用量必须为非负整数", status=422)
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            if reconciliation is not None:
                if row["resolution_json"]:
                    if json.loads(row["resolution_json"]) != reconciliation:
                        raise BizError("LEDGER_RECONCILIATION_CONFLICT", "此请求已核对，重复提交的证据或用量不同", status=409)
                elif reconciliation.get("kind") == "response":
                    if row["status"] != "sent_unknown" or row["response_json"]:
                        raise BizError("LEDGER_STATE_INVALID", "只可补回响应丢失的请求", status=409)
                elif row["status"] != "usage_unknown" or not row["response_json"]:
                    raise BizError("LEDGER_STATE_INVALID", "只可补录已有响应但用量未知的请求", status=409)
                if reconciliation["request_hash"] != row["request_hash"]:
                    raise BizError("CALL_IDENTITY_CONFLICT", "核对指纹与原请求不同", status=409)
                if row["status"] != "ok":
                    response = response if reconciliation.get("kind") == "response" else json.loads(row["response_json"])
                    response["usage"] = {"in": actual_in, "out": actual_out}
            self._refresh(conn, row["run_id"], budget)
            if row["status"] == "ok":
                if (row["actual_in"], row["actual_out"]) != (actual_in, actual_out):
                    raise BizError("LEDGER_SETTLEMENT_CONFLICT", "重复结算用量不一致", status=409)
                return
            if row["status"] not in ("reserved", "sent_unknown", "usage_unknown"):
                raise BizError("LEDGER_STATE_INVALID", "该调用不能结算", status=409)
            actual = actual_in + actual_out
            reservation = row["reserved_tokens"]
            if budget.mode == "money":
                price = json.loads(row["price_json"])
                actual = token_cost(price, actual_in, actual_out)
                reservation = Decimal(row["reserved_amount"])
                actual_cost, currency = str(actual), price["currency"]
            phase = row["phase"]
            budget.reserved[phase] = max(0, budget.reserved.get(phase, 0) - reservation)
            budget.spent[phase] = budget.spent.get(phase, 0) + actual
            conn.execute(
                "UPDATE ledger SET status='ok', actual_in=?, actual_out=?, actual_cost=?, currency=?,"
                " reserved_tokens=0,reserved_amount='0',response_json=? WHERE attempt_id=?",
                (actual_in, actual_out, actual_cost, currency,
                 json_dumps(response) if response is not None else row["response_json"], attempt_id))
            self._persist_budget(conn, row["run_id"], budget)
            if reconciliation is not None:
                conn.execute("UPDATE ledger SET resolution_json=? WHERE attempt_id=?",
                             (json_dumps(reconciliation), attempt_id))
                conn.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                             (new_id("aud"), "local_user", "ledger.reconcile_response" if reconciliation.get("kind") == "response" else "ledger.reconcile_usage", attempt_id,
                              canonical_hash(reconciliation), now_iso()))

    def reconcile_usage(self, run_id: str, attempt_id: str, body: dict) -> dict:
        payload, budget = self._reconciliation_context(run_id, attempt_id, body)
        self.settle(attempt_id, budget, payload["actual_in"], payload["actual_out"], reconciliation=payload)
        return self.reconcile(run_id)

    def reconcile_response(self, run_id: str, attempt_id: str, body: dict) -> dict:
        payload, budget = self._reconciliation_context(run_id, attempt_id, body)
        if not isinstance(body.get("text"), str) or len(body["text"]) > 2000000 or body.get("finish") not in ("stop", "length"):
            raise BizError("RECOVERED_RESPONSE_INVALID", "须提供原始响应文本及stop/length结束原因", status=422)
        usage_known = body.get("usage_known", True)
        if not isinstance(usage_known, bool):
            raise BizError("LEDGER_USAGE_INVALID", "用量是否已核实必须为布尔值", status=422)
        if not usage_known and (payload["actual_in"] is not None or payload["actual_out"] is not None):
            raise BizError("LEDGER_USAGE_INVALID", "用量未核实时不能填写实际token", status=422)
        response = {"text": body["text"], "finish": body["finish"],
                    "usage": {"in": payload["actual_in"], "out": payload["actual_out"]}}
        payload.update(kind="response", response_hash=canonical_hash(response))
        if not usage_known:
            self._save_recovered_unknown_usage(run_id, attempt_id, payload, response, budget)
            return self.reconcile(run_id)
        self.settle(attempt_id, budget, payload["actual_in"], payload["actual_out"],
                    response=response, reconciliation=payload)
        return self.reconcile(run_id)

    def _save_recovered_unknown_usage(self, run_id, attempt_id, payload, response, budget):
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
            if row is None:
                raise BizError("NOT_FOUND", "运行内调用记录不存在", status=404)
            if payload["request_hash"] != row["request_hash"]:
                raise BizError("CALL_IDENTITY_CONFLICT", "核对指纹与原请求不同", status=409)
            if row["response_resolution_json"]:
                if json.loads(row["response_resolution_json"]) != payload:
                    raise BizError("LEDGER_RECONCILIATION_CONFLICT", "补回响应的内容或依据不同", status=409)
                return
            if row["status"] != "sent_unknown" or row["response_json"]:
                raise BizError("LEDGER_STATE_INVALID", "只可补回尚未确认响应的请求", status=409)
            self._refresh(conn, run_id, budget)
            conn.execute("UPDATE ledger SET status='usage_unknown',response_json=?,response_resolution_json=? WHERE attempt_id=?",
                         (json_dumps(response), json_dumps(payload), attempt_id))
            self._persist_budget(conn, run_id, budget)
            conn.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                         (new_id("aud"), "local_user", "ledger.recover_response_usage_unknown", attempt_id,
                          canonical_hash(payload), now_iso()))

    def _reconciliation_context(self, run_id: str, attempt_id: str, body: dict):
        fields = ("reason", "evidence", "request_hash")
        if any(not isinstance(body.get(key), str) or not body[key].strip() or len(body[key]) > 4000 for key in fields):
            raise BizError("RECONCILIATION_EVIDENCE_REQUIRED", "须填写核对原因、供应商记录依据和原请求指纹", status=422)
        row = self.db.one("SELECT run_id FROM ledger WHERE attempt_id=? AND run_id=?", (attempt_id, run_id))
        if row is None:
            raise BizError("NOT_FOUND", "运行内调用记录不存在", status=404)
        run = self.db.one("SELECT budget_state_json FROM runs WHERE id=?", (run_id,))
        if run is None:
            run = self.db.one("SELECT budget_state_json FROM severity_audits WHERE id=?", (run_id,))
        if run is None:
            raise BizError("NOT_FOUND", "运行不存在", status=404)
        payload = {key: body[key].strip() for key in fields}
        payload.update(actual_in=body.get("actual_in"), actual_out=body.get("actual_out"))
        return payload, BudgetState(json.loads(run["budget_state_json"]))

    def reconcile_not_accepted(self, run_id: str, attempt_id: str, body: dict) -> dict:
        payload, budget = self._reconciliation_context(run_id, attempt_id, body)
        if body.get("confirmed_not_accepted") is not True or any(payload[key] is not None for key in ("actual_in", "actual_out")):
            raise BizError("NOT_ACCEPTED_CONFIRMATION_REQUIRED", "须明确确认供应商未受理，不能同时填写实际用量", status=422)
        payload.update(kind="not_accepted", confirmed_not_accepted=True)
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
            if row is None:
                raise BizError("NOT_FOUND", "运行内调用记录不存在", status=404)
            if payload["request_hash"] != row["request_hash"]:
                raise BizError("CALL_IDENTITY_CONFLICT", "核对指纹与原请求不同", status=409)
            if row["resolution_json"]:
                if json.loads(row["resolution_json"]) != payload:
                    raise BizError("LEDGER_RECONCILIATION_CONFLICT", "此请求已有不同核对记录", status=409)
            else:
                if row["status"] != "sent_unknown" or row["response_json"]:
                    raise BizError("LEDGER_STATE_INVALID", "只可核对响应未确认的旧请求", status=409)
                conn.execute("UPDATE ledger SET status='failed',reserved_tokens=0,reserved_amount='0',actual_in=0,actual_out=0,actual_cost='0',resolution_json=? WHERE attempt_id=?",
                             (json_dumps(payload), attempt_id))
                self._refresh(conn, run_id, budget)
                self._persist_budget(conn, run_id, budget)
                conn.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                             (new_id("aud"), "local_user", "ledger.confirm_not_accepted", attempt_id, canonical_hash(payload), now_iso()))
        return self.reconcile(run_id)

    def authorize_retry(self, run_id: str, attempt_id: str, body: dict) -> dict:
        payload, _ = self._reconciliation_context(run_id, attempt_id, body)
        if body.get("confirmed_possible_duplicate") is not True or any(payload[key] is not None for key in ("actual_in", "actual_out")):
            raise BizError("RETRY_CONFIRMATION_REQUIRED", "须确认新尝试可能重复计费，旧费用继续保留", status=422)
        payload.update(kind="retry", confirmed_possible_duplicate=True)
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
            if row is None or row["request_hash"] != payload["request_hash"]:
                raise BizError("CALL_IDENTITY_CONFLICT", "请求不存在或核对指纹不符", status=409)
            if row["retry_authorization_json"]:
                if json.loads(row["retry_authorization_json"]) != payload:
                    raise BizError("LEDGER_RECONCILIATION_CONFLICT", "已有不同重试授权", status=409)
            else:
                latest = conn.execute("SELECT attempt_id FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index DESC LIMIT 1", (run_id, row["logical_id"])).fetchone()
                saved = conn.execute("SELECT 1 FROM ledger WHERE run_id=? AND logical_id=? AND status IN ('ok','usage_unknown') AND response_json<>''", (run_id, row["logical_id"])).fetchone()
                if saved or latest[0] != attempt_id or row["status"] != "sent_unknown" or row["response_json"]:
                    raise BizError("LEDGER_STATE_INVALID", "只能授权最近一次结果未知的请求", status=409)
                conn.execute("UPDATE ledger SET retry_authorization_json=? WHERE attempt_id=?", (json_dumps(payload), attempt_id))
                conn.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                             (new_id("aud"), "local_user", "ledger.authorize_retry", attempt_id, canonical_hash(payload), now_iso()))
        return self.reconcile(run_id)

    def mark_sent_unknown(self, attempt_id: str, budget: BudgetState) -> None:
        """供应商已接收但响应丢失：保留预留作为潜在费用，不计入确定支出（TC040）。"""
        self._mark_unknown(attempt_id, budget, "sent_unknown")

    def mark_usage_unknown(self, attempt_id: str, budget: BudgetState, response: dict | None = None) -> None:
        """输出已收到，用量无法确认：保留预留，与响应丢失分开记录。"""
        self._mark_unknown(attempt_id, budget, "usage_unknown", response)

    def _mark_unknown(self, attempt_id: str, budget: BudgetState, status: str, response: dict | None = None) -> None:
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            self._refresh(conn, row["run_id"], budget)
            if row["status"] != "reserved":
                return
            conn.execute("UPDATE ledger SET status=?,response_json=? WHERE attempt_id=?",
                         (status, json_dumps(response) if response is not None else row["response_json"], attempt_id))
            # 预留保留在 reserved 中，不归零（潜在费用），也不进 spent
            self._persist_budget(conn, row["run_id"], budget)

    def mark_failed(self, attempt_id: str, budget: BudgetState) -> None:
        """请求未发出或供应商明确拒绝：释放预留，不计费。"""
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            self._refresh(conn, row["run_id"], budget)
            if row["status"] != "reserved":
                return
            conn.execute("UPDATE ledger SET status='failed', reserved_tokens=0,reserved_amount='0' WHERE attempt_id=?",
                         (attempt_id,))
            budget.reserved[row["phase"]] = max(
                0, budget.reserved.get(row["phase"], 0) -
                (Decimal(row["reserved_amount"]) if budget.mode == "money" else int(row["reserved_tokens"])))
            self._persist_budget(conn, row["run_id"], budget)

    def reconcile(self, run_id: str) -> dict:
        """对账（TC057）：各 attempt 求和与账本一致，sent_unknown 余额单列。"""
        rows = self.db.query("SELECT * FROM ledger WHERE run_id=?", (run_id,))
        by_role: dict = {}
        unknown = 0
        known = 0
        money = {}
        for r in rows:
            ent = by_role.setdefault(r["role"], {"requests": 0, "tokens": 0, "unknown": 0})
            ent["requests"] += 1
            if r["status"] == "ok":
                ent["tokens"] += (r["actual_in"] or 0) + (r["actual_out"] or 0)
                known += (r["actual_in"] or 0) + (r["actual_out"] or 0)
            elif r["status"] in ("sent_unknown", "usage_unknown", "reserved"):
                ent["unknown"] += r["reserved_tokens"]
                unknown += r["reserved_tokens"]
            price = json.loads(r["price_json"] or "{}")
            if price:
                amounts = money.setdefault(price["currency"], {"known": Decimal(0), "reserved": Decimal(0)})
                if r["status"] == "ok" and r["actual_cost"]:
                    amounts["known"] += Decimal(r["actual_cost"])
                elif r["status"] in ("reserved", "sent_unknown", "usage_unknown"):
                    amounts["reserved"] += Decimal(r["reserved_amount"])
        total = known + unknown
        spent = sum(1 for r in rows if r["status"] == "ok")
        return {"by_role": by_role, "known_tokens": known, "unknown_tokens": unknown,
                "total_upper_bound": total, "attempts": len(rows), "settled_attempts": spent,
                "consistent": all(r["status"] in ("ok", "sent_unknown", "usage_unknown", "reserved", "failed") for r in rows),
                "pending_attempts": sum(r["status"] == "reserved" for r in rows),
                "usage_unknown_attempts": sum(r["status"] == "usage_unknown" for r in rows),
                "money_by_currency": {currency: {key: str(value) for key, value in amounts.items()}
                                      for currency, amounts in money.items()}}

    def phase_summary(self, run_id: str, phase: str) -> dict:
        """Freeze physical request and cost evidence for one report phase.

        Unknown attempts retain their reservation as an upper bound; configured
        prices multiplied by reported usage are estimates, not provider invoices.
        """
        if phase not in ("search", "acceptance"):
            raise ValueError("unsupported ledger phase")
        rows = self.db.query(
            "SELECT role,status,reserved_tokens,actual_in,actual_out,reserved_amount,"
            "actual_cost,currency,price_json FROM ledger WHERE run_id=? AND phase=? ORDER BY created_at,rowid",
            (run_id, phase))
        pending_states = {"reserved", "sent_unknown", "usage_unknown"}
        by_role = {}
        known_tokens = unknown_tokens = 0
        actual_cost = {}
        reserved_cost = {}
        unpriced_settled = 0
        for row in rows:
            role = row["role"]
            entry = by_role.setdefault(role, {"attempts": 0, "settled": 0, "failed": 0,
                                               "pending": 0, "known_tokens": 0,
                                               "unknown_token_reservation": 0})
            entry["attempts"] += 1
            status = row["status"]
            if status == "ok":
                entry["settled"] += 1
                tokens = (row["actual_in"] or 0) + (row["actual_out"] or 0)
                entry["known_tokens"] += tokens
                known_tokens += tokens
            elif status == "failed":
                entry["failed"] += 1
            elif status in pending_states:
                entry["pending"] += 1
                reservation = row["reserved_tokens"] or 0
                entry["unknown_token_reservation"] += reservation
                unknown_tokens += reservation

            price = json.loads(row["price_json"] or "{}")
            currency = price.get("currency") or row["currency"]
            if not currency:
                if status == "ok":
                    unpriced_settled += 1
                continue
            if status == "ok" and row["actual_cost"] not in (None, ""):
                actual_cost[currency] = actual_cost.get(currency, Decimal(0)) + Decimal(row["actual_cost"])
            elif status in pending_states and row["reserved_amount"] not in (None, ""):
                reserved_cost[currency] = reserved_cost.get(currency, Decimal(0)) + Decimal(row["reserved_amount"])

        return {
            "phase": phase,
            "physical_attempts": len(rows),
            "settled_attempts": sum(v["settled"] for v in by_role.values()),
            "failed_attempts": sum(v["failed"] for v in by_role.values()),
            "pending_attempts": sum(v["pending"] for v in by_role.values()),
            "known_tokens": known_tokens,
            "unknown_token_reservation": unknown_tokens,
            "actual_cost_estimate_by_currency": {k: str(v) for k, v in actual_cost.items()},
            "reserved_cost_upper_bound_by_currency": {k: str(v) for k, v in reserved_cost.items()},
            "unpriced_settled_attempts": unpriced_settled,
            "by_role": by_role,
            "cost_basis": "冻结单价×供应商报告用量；未知结果保留预算预留。不是供应商账单。",
        }


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False, sort_keys=True)

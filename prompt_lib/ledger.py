"""预算与调用账本（D08 / TC032—TC035, TC040, TC057）。

每次请求：logical_request_id + attempt_id。派发前以原子更新预留预计上限；
成功写 actual 并释放差额；供应商已接收但响应丢失标 sent_unknown 保留潜在费用，
不无限重试；价格未知时禁止金额硬预算，只允许明确 token 上限模式。
"""
from __future__ import annotations

from .core import BizError, new_id, now_iso
from .db import DB


class BudgetState:
    """运行预算状态：spent / reserved 按 phase 累计。"""

    def __init__(self, data: dict):
        self.mode = data.get("mode", "token")            # token / money
        self.total_limit = int(data.get("total_limit") or 0)
        self.search_limit = int(data.get("search_limit") or 0)
        self.acceptance_limit = int(data.get("acceptance_limit") or 0)
        self.spent = data.get("spent") or {"search": 0, "acceptance": 0}
        self.reserved = data.get("reserved") or {"search": 0, "acceptance": 0}

    def to_json(self) -> dict:
        return {"mode": self.mode, "total_limit": self.total_limit,
                "search_limit": self.search_limit, "acceptance_limit": self.acceptance_limit,
                "spent": self.spent, "reserved": self.reserved}

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


class Ledger:
    def __init__(self, db: DB):
        self.db = db

    def reserve(self, run_id: str, logical_id: str, role: str, model: str, phase: str,
                est_tokens: int, budget: BudgetState, request_hash: str = "") -> str:
        """原子预留：spent + reserved + est <= phase_limit，否则 BUDGET_EXHAUSTED。"""
        with self.db.tx() as conn:
            phase_spent = int(budget.spent.get(phase, 0))
            phase_reserved = int(budget.reserved.get(phase, 0))
            if phase_spent + phase_reserved + est_tokens > budget.phase_limit(phase):
                raise BizError("BUDGET_EXHAUSTED",
                               f"阶段[{phase}]预算不足：已用{phase_spent}+在途{phase_reserved}"
                               f"+本次预计{est_tokens}超过上限{budget.phase_limit(phase)}；"
                               "独立验收预留不会被搜索占用（TC033）")
            if (budget.spent.get("search", 0) + budget.spent.get("acceptance", 0)
                    + budget.reserved.get("search", 0) + budget.reserved.get("acceptance", 0)
                    + est_tokens > budget.total_limit):
                raise BizError("BUDGET_EXHAUSTED", "总预算不足，运行将暂停")
            budget.reserved[phase] = phase_reserved + est_tokens
            attempt_id = new_id("atm")
            conn.execute(
                "INSERT INTO ledger(id, run_id, logical_id, attempt_id, attempt_index, role, model,"
                " phase, reserved_tokens, status, request_hash, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?, 'reserved', ?, ?)",
                (attempt_id, run_id, logical_id, attempt_id,
                 self._next_attempt_index(conn, run_id, logical_id), role, model, phase,
                 est_tokens, request_hash, now_iso()))
            conn.execute("UPDATE runs SET budget_state_json=?, updated_at=? WHERE id=?",
                         (json_dumps(budget.to_json()), now_iso(), run_id))
            return attempt_id

    @staticmethod
    def _next_attempt_index(conn, run_id: str, logical_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_index),0)+1 AS n FROM ledger WHERE run_id=? AND logical_id=?",
            (run_id, logical_id)).fetchone()
        return row["n"]

    def settle(self, attempt_id: str, budget: BudgetState, actual_in: int, actual_out: int,
               currency: str = "", actual_cost: str = "") -> None:
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            actual = actual_in + actual_out
            delta = int(row["reserved_tokens"]) - actual
            phase = row["phase"]
            budget.reserved[phase] = max(0, budget.reserved.get(phase, 0) - max(0, int(row["reserved_tokens"])))
            budget.spent[phase] = budget.spent.get(phase, 0) + actual
            conn.execute(
                "UPDATE ledger SET status='ok', actual_in=?, actual_out=?, actual_cost=?, currency=?,"
                " reserved_tokens=0 WHERE attempt_id=?",
                (actual_in, actual_out, actual_cost, currency, attempt_id))
            conn.execute("UPDATE runs SET budget_state_json=?, updated_at=? WHERE id=?",
                         (json_dumps(budget.to_json()), now_iso(), row["run_id"]))

    def mark_sent_unknown(self, attempt_id: str, budget: BudgetState) -> None:
        """供应商已接收但响应丢失：保留预留作为潜在费用，不计入确定支出（TC040）。"""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE ledger SET status='sent_unknown' WHERE attempt_id=?", (attempt_id,))
            # 预留保留在 reserved 中，不归零（潜在费用），也不进 spent
            conn.execute("UPDATE runs SET budget_state_json=?, updated_at=? WHERE id=?",
                         (json_dumps(budget.to_json()), now_iso(), row["run_id"]))

    def mark_failed(self, attempt_id: str, budget: BudgetState) -> None:
        """请求未发出或供应商明确拒绝：释放预留，不计费。"""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE ledger SET status='failed', reserved_tokens=0 WHERE attempt_id=?",
                         (attempt_id,))
            budget.reserved[row["phase"]] = max(
                0, budget.reserved.get(row["phase"], 0) - int(row["reserved_tokens"]))
            conn.execute("UPDATE runs SET budget_state_json=?, updated_at=? WHERE id=?",
                         (json_dumps(budget.to_json()), now_iso(), row["run_id"]))

    def reconcile(self, run_id: str) -> dict:
        """对账（TC057）：各 attempt 求和与账本一致，sent_unknown 余额单列。"""
        rows = self.db.query("SELECT * FROM ledger WHERE run_id=?", (run_id,))
        by_role: dict = {}
        unknown = 0
        known = 0
        for r in rows:
            ent = by_role.setdefault(r["role"], {"requests": 0, "tokens": 0, "unknown": 0})
            ent["requests"] += 1
            if r["status"] == "ok":
                ent["tokens"] += (r["actual_in"] or 0) + (r["actual_out"] or 0)
                known += (r["actual_in"] or 0) + (r["actual_out"] or 0)
            elif r["status"] == "sent_unknown":
                ent["unknown"] += r["reserved_tokens"]
                unknown += r["reserved_tokens"]
        total = known + unknown
        spent = sum(1 for r in rows if r["status"] == "ok")
        return {"by_role": by_role, "known_tokens": known, "unknown_tokens": unknown,
                "total_upper_bound": total, "attempts": len(rows), "settled_attempts": spent,
                "consistent": spent + sum(1 for r in rows if r["status"] == "sent_unknown") == len(rows)}


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False, sort_keys=True)

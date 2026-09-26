"""运行服务：快照、幂等、执行、取消、候选锁定、独立验收、发布与反馈（D06/D09/D10）。"""
from __future__ import annotations

import json
import threading

from .core import BizError, canonical_hash, new_id, now_iso
from .db import DB, get_db
from .domain import DataService, ProjectsService, jloads
from .engine import generate_once, score_prompt
from .ledger import BudgetState, Ledger, validate_money_budget_needs_prices
from .stats import (exact_mcnemar, group_outcomes, missing_bounds, paired_bootstrap,
                    zero_event_upper_bound)

_engine_threads: dict[str, threading.Thread] = {}


def _settings(key: str, default):
    db = get_db()
    row = db.one("SELECT value_json FROM settings WHERE key=?", (key,))
    return json.loads(row["value_json"]) if row else default


class RunService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    # ---------------- 快照校验（无收费）
    def validate_snapshot(self, pid: str, draft: dict) -> dict:
        ProjectsService(self.db).ensure_active(pid)
        errors, normalized = {}, dict(draft)

        def check_ref(value, table, field, label):
            if not isinstance(value, str) or not value:
                errors[field] = f"{label}必须为明确的版本ID"
                return None
            if "latest" in value.lower():
                errors[field] = f"{label}禁止引用latest（TC031）"
                return None
            if self.db.one(f"SELECT 1 FROM {table} WHERE id=?", (value,)) is None:
                errors[field] = f"{label}不存在：{value}"
                return None
            return value

        prompt = draft.get("prompt") or {}
        check_ref(prompt.get("baseline_id"), "prompt_versions", "prompt.baseline_id", "基线提示词版本")
        check_ref(draft.get("manifest_id"), "split_manifests", "manifest_id", "切分清单")
        check_ref(draft.get("rubric_id"), "rubrics", "rubric_id", "评价标准版本")
        judge_id = draft.get("judge_id")
        mode = draft.get("mode", "explore")
        if mode not in ("explore", "batch"):
            errors["mode"] = "mode 必须是 explore 或 batch"
        if judge_id:
            check_ref(judge_id, "judges", "judge_id", "评价器")
            if not errors.get("judge_id"):
                jr = self.db.one("SELECT status FROM judges WHERE id=?", (judge_id,))
                if jr["status"] == "stale":
                    errors["judge_id"] = "评价器已stale：标准或模型变化后需重新校准（TC024）"
                elif jr["status"] != "audited" and mode == "batch":
                    errors["judge_id"] = "批量模式需要已审计(audited)的评价器；可改用explore模式"
        elif mode == "batch":
            errors["judge_id"] = "批量模式需要评价器；或改用explore模式（允许退回人工评价）"
        models = draft.get("models") or {}
        for role in ("generation", "evaluation", "optimizer"):
            mc = models.get(role)
            if not mc or not mc.get("connection_id"):
                errors[f"models.{role}"] = "必须配置模型连接（可使用内置离线模拟供应商）"
        # 预算类配置错误直接抛出，保留具体错误码（TC032/TC033）
        budget = draft.get("budget") or {}
        b = BudgetState(budget)
        b.validate()
        validate_money_budget_needs_prices(budget, models, _settings("prices", {}))
        manifest = None
        if not errors.get("manifest_id"):
            man = self.db.one("SELECT * FROM split_manifests WHERE id=? AND project_id=?",
                              (draft.get("manifest_id"), pid))
            if man is None:
                errors["manifest_id"] = "切分清单不属于该项目"
            else:
                manifest = man
        dev_ids = (draft.get("data") or {}).get("dev_item_ids") or []
        if not dev_ids:
            errors["data.dev_item_ids"] = "必须选择开发集案例"
        elif manifest is not None:
            frozen = {r["id"] for r in self.db.query(
                "SELECT id FROM dataset_items WHERE project_id=? AND split IN ('dev','select')", (pid,))}
            bad = [i for i in dev_ids if i not in frozen]
            if bad:
                errors["data.dev_item_ids"] = f"以下案例不在已冻结的dev/select集合中：{bad[:5]}"
        if errors:
            raise BizError("SNAPSHOT_INVALID", "实验配置存在缺项，请按字段定位修正",
                           field_errors=errors, status=422)
        normalized["project_id"] = pid
        normalized["package_lock"] = {"engine": "prompt-lab", "version": "0.1.0"}
        normalized["acceptance"] = (draft.get("acceptance") or {
            "use_sealed_test": True, "n_min_groups": 2})
        return normalized

    def estimate(self, draft: dict) -> dict:
        """费用预估区间：基于样本量与每请求token假设，不含校准与重试（开发方案第8节）。"""
        dev_n = len((draft.get("data") or {}).get("dev_item_ids") or [])
        cand = int((draft.get("optimization") or {}).get("max_candidates") or 0)
        gen_calls = dev_n * (1 + cand)
        eval_calls = gen_calls
        opt_calls = cand
        gen_in, gen_out = 1800, 800
        ev_in, ev_out = 2600, 350
        op_in, op_out = 6000, 1600
        total = (gen_calls * (gen_in + gen_out) + eval_calls * (ev_in + ev_out)
                 + opt_calls * (op_in + op_out))
        return {"generation_calls": gen_calls, "evaluation_calls": eval_calls,
                "optimizer_calls": opt_calls, "estimated_tokens": total,
                "note": "为说明计算方法的假设区间，非报价；由硬预算控制上限"}

    # ---------------- 创建（幂等 TC035）
    def create_run(self, pid: str, snapshot: dict, idempotency_key: str = "") -> dict:
        payload_hash = canonical_hash(snapshot)
        if idempotency_key:
            row = self.db.one("SELECT * FROM runs WHERE project_id=? AND idempotency_key=?",
                              (pid, idempotency_key))
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise BizError("IDEMPOTENCY_CONFLICT",
                                   "同一Idempotency-Key但配置内容不同：请更换键或确认配置（TC035）",
                                   status=409)
                return self.get(row["id"])
        rid = new_id("run")
        sh = canonical_hash(snapshot)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO runs(id,project_id,idempotency_key,payload_hash,snapshot_json,"
                "snapshot_hash,state,baseline_prompt_id,budget_state_json,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?, 'queued', ?, ?, ?, ?)",
                (rid, pid, idempotency_key, payload_hash, json.dumps(snapshot, ensure_ascii=False),
                 sh, snapshot["prompt"]["baseline_id"],
                 json.dumps(BudgetState(snapshot["budget"]).to_json(), ensure_ascii=False),
                 now_iso(), now_iso()))
            self._emit(conn, rid, 1, "created", {"snapshot_hash": sh})
        self._audit("run.create", rid, sh)
        return self.get(rid)

    def get(self, rid: str) -> dict:
        r = self.db.one("SELECT * FROM runs WHERE id=?", (rid,))
        if r is None:
            raise BizError("NOT_FOUND", "运行不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"], "state": r["state"],
                "stage": r["stage"], "stop_reason": r["stop_reason"], "revision": r["revision"],
                "snapshot": jloads(r["snapshot_json"], {}), "snapshot_hash": r["snapshot_hash"],
                "baseline_prompt_id": r["baseline_prompt_id"],
                "baseline_score": r["baseline_score"],
                "candidates": jloads(r["candidates_json"], []),
                "locked_candidate": r["locked_candidate"],
                "budget": jloads(r["budget_state_json"], {}),
                "error": r["error"], "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in
                self.db.query("SELECT id FROM runs WHERE project_id=? ORDER BY created_at DESC",
                              (pid,))]

    # ---------------- 执行（后台线程；协作式取消 TC038；预算暂停）
    def start(self, rid: str) -> None:
        run = self.get(rid)
        if run["state"] not in ("queued", "paused_budget"):
            raise BizError("RUN_STATE_INVALID", f"当前状态 {run['state']} 不能启动", status=409)
        self.db.execute("UPDATE runs SET state='running', stop_reason='', error='' WHERE id=?", (rid,))
        t = threading.Thread(target=self._execute, args=(rid,), daemon=True)
        _engine_threads[rid] = t
        t.start()

    def _cancelled(self, rid: str) -> bool:
        r = self.db.one("SELECT state FROM runs WHERE id=?", (rid,))
        return r is None or r["state"] in ("stopping", "cancelled")

    def _set_state(self, rid: str, state: str, stage: str = "", stop_reason: str = "") -> None:
        self.db.execute("UPDATE runs SET state=?, stage=?, stop_reason=?, updated_at=? WHERE id=?",
                        (state, stage, stop_reason, now_iso(), rid))

    def _execute(self, rid: str) -> None:
        run = self.get(rid)
        snapshot = run["snapshot"]
        budget = BudgetState(run["budget"])
        ledger = Ledger(self.db)
        project = ProjectsService(self.db).get(run["project_id"])
        try:
            with self.db.tx() as conn:
                self._emit(conn, rid, None, "stage", {"stage": "baseline"})
            self._set_state(rid, "running", "baseline")
            dev_ids = snapshot["data"]["dev_item_ids"]
            sample = dev_ids[: int(snapshot.get("optimization", {}).get("dev_sample_size") or len(dev_ids))]
            pv = self.db.one("SELECT * FROM prompt_versions WHERE id=?",
                             (snapshot["prompt"]["baseline_id"],))
            from .domain import PromptService
            baseline_pv = PromptService(self.db)._row(pv)
            base_res = score_prompt(self.db, project, baseline_pv, sample,
                                    snapshot["rubric_id"], snapshot["models"], rid,
                                    "search", budget, ledger)
            with self.db.tx() as conn:
                self._emit(conn, rid, None, "baseline_done", {k: base_res[k] for k in
                             ("score", "usable_rate", "n", "n_scored", "severe")})
            candidates = []
            if self._cancelled(rid):
                raise BizError("CANCELLED", "运行已取消")
            max_cand = int(snapshot.get("optimization", {}).get("max_candidates") or 0)
            for i in range(max_cand):
                if self._cancelled(rid):
                    raise BizError("CANCELLED", "运行已取消：停止派发新请求，在途结果仍入账（TC038）")
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "stage", {"stage": f"candidate_{i + 1}"})
                from .engine import propose_fragment
                fragment = propose_fragment(self.db, snapshot["models"].get("optimizer")
                                            or snapshot["models"]["generation"],
                                            base_res["failures"], rid, budget, ledger)
                cand_body = baseline_pv["body"].rstrip() + "\n\n" + fragment
                cand_pv = PromptService(self.db).create_version(
                    run["project_id"], baseline_pv["name"], cand_body,
                    baseline_pv["frozen_segments"], baseline_pv["variables"],
                    baseline_pv["params"], parent_id=baseline_pv["id"], origin="optimizer",
                    hypothesis=f"候选{i + 1}：按失败证据追加输出要求片段")
                cand_res = score_prompt(self.db, project, cand_pv, sample, snapshot["rubric_id"],
                                        snapshot["models"], rid, "search", budget, ledger)
                candidates.append({"candidate_id": f"cand_{i + 1}", "prompt_version_id": cand_pv["id"],
                                   "hash": cand_pv["hash"], "parent_id": baseline_pv["id"],
                                   "score": cand_res["score"], "usable_rate": cand_res["usable_rate"],
                                   "severe": cand_res["severe"],
                                   "evidence": cand_res["failures"][:5]})
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "candidate_done",
                               {"candidate_id": f"cand_{i + 1}", "score": cand_res["score"]})
            best = max(candidates, key=lambda c: c["score"]) if candidates else None
            min_delta = float(snapshot.get("optimization", {}).get("min_delta") or 0.0)
            locked = ""
            stop_reason = "no_improvement"
            if best and best["score"] > base_res["score"] + min_delta:
                stop_reason = "candidate_found"
            self.db.execute("UPDATE runs SET candidates_json=?, locked_candidate=?,"
                            " baseline_score=?, updated_at=? WHERE id=?",
                            (json.dumps(candidates, ensure_ascii=False), locked,
                             base_res["score"], now_iso(), rid))
            if self._cancelled(rid):
                self._set_state(rid, "cancelled", stop_reason="user_cancelled")
            else:
                self._set_state(rid, "completed", "done", stop_reason)
            with self.db.tx() as conn:
                self._emit(conn, rid, None, "completed",
                           {"stop_reason": stop_reason,
                            "baseline_score": base_res["score"],
                            "best_score": best["score"] if best else None,
                            "note": "无提升为合法结果：保留基线（TC037）" if stop_reason == "no_improvement" else ""})
        except BizError as e:
            if e.code == "BUDGET_EXHAUSTED":
                self._set_state(rid, "paused_budget", stop_reason="budget_exhausted")
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "paused", {"reason": e.message})
            elif e.code == "CANCELLED":
                self._set_state(rid, "cancelled", stop_reason="user_cancelled")
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "cancelled", {})
            else:
                self.db.execute("UPDATE runs SET state='failed', error=?, updated_at=? WHERE id=?",
                                (e.message, now_iso(), rid))
        except Exception as e:  # 未知异常：状态可解释，不静默
            self.db.execute("UPDATE runs SET state='failed', error=?, updated_at=? WHERE id=?",
                            (f"内部错误：{e}", now_iso(), rid))

    def _emit(self, conn, rid: str, seq, etype: str, payload: dict) -> None:
        row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM run_events WHERE run_id=?",
                           (rid,)).fetchone()
        conn.execute("INSERT INTO run_events(run_id,seq,type,payload_json,created_at)"
                     " VALUES(?,?,?,?,?)",
                     (rid, row["n"], etype, json.dumps(payload, ensure_ascii=False), now_iso()))

    def events(self, rid: str, cursor: int = 0) -> list[dict]:
        rows = self.db.query("SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq",
                             (rid, cursor))
        return [{"seq": r["seq"], "type": r["type"], "payload": jloads(r["payload_json"], {}),
                 "created_at": r["created_at"]} for r in rows]

    def cancel(self, rid: str, revision: int) -> dict:
        run = self.get(rid)
        if run["revision"] != revision:
            raise BizError("REVISION_CONFLICT", "运行状态已变化，请刷新后重试", status=409)
        if run["state"] in ("completed", "cancelled", "failed"):
            return run
        self.db.execute("UPDATE runs SET state='stopping', revision=revision+1, updated_at=?"
                        " WHERE id=?", (now_iso(), rid))
        self._audit("run.cancel", rid, "")
        return self.get(rid)

    def resume(self, rid: str) -> dict:
        run = self.get(rid)
        if run["state"] != "paused_budget":
            raise BizError("RUN_STATE_INVALID", "只有预算暂停的运行可以恢复", status=409)
        self.start(rid)
        return self.get(rid)

    def lock_candidate(self, rid: str, candidate_id: str) -> dict:
        run = self.get(rid)
        if run["state"] != "completed":
            raise BizError("RUN_STATE_INVALID", "只有已完成的运行可以锁定候选", status=409)
        if run["locked_candidate"] and run["locked_candidate"] != candidate_id:
            raise BizError("LOCK_EXISTS", "一次运行只能锁定一个挑战者（TC042）", status=409)
        ids = {c["candidate_id"] for c in run["candidates"]} | {"baseline"}
        if candidate_id not in ids:
            raise BizError("CANDIDATE_UNKNOWN", f"未知候选：{candidate_id}")
        self.db.execute("UPDATE runs SET locked_candidate=?, updated_at=? WHERE id=?",
                        (candidate_id, now_iso(), rid))
        return self.get(rid)

    def ledger_view(self, rid: str) -> dict:
        return Ledger(self.db).reconcile(rid)

    def _audit(self, action: str, target: str, payload_hash: str) -> None:
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", action, target, payload_hash, now_iso()))


# ---------------------------------------------------------------- 独立验收 P12

class AcceptanceService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def accept(self, rid: str) -> dict:
        """锁定候选后解封运行（TC042）；测试明细已消耗则拒绝（TC043）。"""
        run = RunService(self.db).get(rid)
        if run["state"] != "completed":
            raise BizError("RUN_STATE_INVALID", "运行尚未完成", status=409)
        if not run["locked_candidate"]:
            raise BizError("CANDIDATE_NOT_LOCKED", "必须先锁定最终候选才能申请独立验收（TC042）",
                           status=409)
        snapshot = run["snapshot"]
        # 评价器有效性：stale评价器不能做自动确认结论（BR10/TC024）
        judge_id = snapshot.get("judge_id")
        if judge_id:
            jr = self.db.one("SELECT status FROM judges WHERE id=?", (judge_id,))
            if jr and jr["status"] == "stale":
                raise BizError("JUDGE_STALE", "评价器已stale，不能用于确认性验收", status=422)
        # 解封（消耗性）
        sealed = DataService(self.db).unseal_for_acceptance(run["project_id"])
        if not sealed:
            raise BizError("TEST_ALREADY_CONSUMED",
                           "封存测试集不存在或已消耗：测试明细用于改写后不能再次作为独立证明（TC043）",
                           status=409)
        candidates = {c["candidate_id"]: c for c in run["candidates"]}
        if run["locked_candidate"] == "baseline":
            cand_pv_id = run["baseline_prompt_id"]
        else:
            cand_pv_id = candidates[run["locked_candidate"]]["prompt_version_id"]
        baseline_pv_id = run["baseline_prompt_id"]
        models = snapshot["models"]
        budget = BudgetState(run["budget"])
        ledger = Ledger(self.db)
        project = ProjectsService(self.db).get(run["project_id"])
        from .domain import PromptService
        pv_base = PromptService(self.db).get(baseline_pv_id)
        pv_cand = PromptService(self.db).get(cand_pv_id)
        rubric_id = snapshot["rubric_id"]

        pairs = []
        severe_base = severe_cand = unknown = 0
        for art in sealed:
            item = {"id": art["item_id"], "case_id": art["item_id"],
                    "runtime_input": art["runtime_input"], "source_group_id": art["item_id"]}
            # 交错执行：基线与候选同输入/同参数/同模型（TC042）
            ob = generate_once(self.db, project, pv_base, item, models["generation"], rid,
                               "acceptance", budget, ledger)
            oc = generate_once(self.db, project, pv_cand, item, models["generation"], rid,
                               "acceptance", budget, ledger)
            from .engine import evaluate_once
            from .providers import has_severe_error, is_usable
            row_b = self.db.one("SELECT text FROM outputs WHERE id=?", (ob["id"],))
            row_c = self.db.one("SELECT text FROM outputs WHERE id=?", (oc["id"],))
            sb = evaluate_once(row_b["text"], rubric_id, models["evaluation"], rid, "acceptance") \
                if ob["status"] == "ok" else {"abstain": True}
            sc = evaluate_once(row_c["text"], rubric_id, models["evaluation"], rid, "acceptance") \
                if oc["status"] == "ok" else {"abstain": True}
            entry = {"item_id": art["item_id"], "base_ok": ob["status"] == "ok",
                     "cand_ok": oc["status"] == "ok",
                     "base_usable": is_usable(sb) if not sb.get("abstain") else None,
                     "cand_usable": is_usable(sc) if not sc.get("abstain") else None,
                     "base_severe": has_severe_error(row_b["text"], sb) if not sb.get("abstain")
                     and ob["status"] == "ok" else None,
                     "cand_severe": has_severe_error(row_c["text"], sc) if not sc.get("abstain")
                     and oc["status"] == "ok" else None}
            if entry["base_severe"]:
                severe_base += 1
            if entry["cand_severe"]:
                severe_cand += 1
            if entry["base_usable"] is None or entry["cand_usable"] is None:
                unknown += 1
            pairs.append(entry)

        known = [p for p in pairs if p["base_usable"] is not None and p["cand_usable"] is not None]
        g = [(p["item_id"], p["base_usable"], p["cand_usable"]) for p in known]
        both = sum(1 for _, b, c in g if b and c)
        cand_only = sum(1 for _, b, c in g if c and not b)
        base_only = sum(1 for _, b, c in g if b and not c)
        neither = sum(1 for _, b, c in g if not b and not c)
        n = len(g)
        base_rate = (both + base_only) / n if n else 0.0
        cand_rate = (both + cand_only) / n if n else 0.0
        diff = cand_rate - base_rate
        p_mcnemar = exact_mcnemar(cand_only, base_only)
        boot = paired_bootstrap([[1.0 if b else 0.0] for _, b, c in g],
                                [[1.0 if c else 0.0] for _, b, c in g],
                                n_resamples=20000, seed=20260927)
        mb = missing_bounds(sum(1 for p in pairs if p["cand_usable"]), 0, unknown)
        sev_upper = zero_event_upper_bound(n) if severe_cand == 0 else None
        policy = snapshot.get("acceptance", {}).get("policy") or \
            project["contract"].get("acceptance_policy") or {}
        min_impr = float(policy.get("min_observed_improvement", 0.05))

        if n == 0 or boot.get("degenerate") and abs(diff) < 1e-12 and n < 2:
            decision = "inconclusive"
        elif unknown > 0 and unknown / max(1, len(pairs)) > 0.2:
            decision = "inconclusive"  # 缺失过多：证据不足，不静默删除（TC046/TC047）
        elif severe_cand > 0:
            decision = "regression"    # 均分提高也不能覆盖严重错误（TC046）
        elif diff >= min_impr and boot["ci_low"] > 0:
            decision = "verified_improvement"
        elif boot["ci_high"] < 0:
            decision = "regression"
        else:
            decision = "no_improvement"
        stats = {
            "group_n": n, "sealed_total": len(sealed), "unknown": unknown,
            "baseline_usable_rate": base_rate, "candidate_usable_rate": cand_rate,
            "diff": diff, "fix": cand_only, "regress": base_only, "both": both, "neither": neither,
            "mcnemar_p": p_mcnemar, "bootstrap": boot,
            "missing_bounds": mb, "severe_baseline": severe_base, "severe_candidate": severe_cand,
            "severe_upper_bound_95": sev_upper,
            "severe_note": ("候选严重错误为0观察：单侧95%上界约{:.2%}，不等于真实零风险（TC047）"
                            .format(sev_upper) if sev_upper is not None else ""),
            "policy": policy, "primary_metric": policy.get("primary_metric") or
            project["contract"].get("primary_metric"),
        }
        rep_id = new_id("rep")
        self.db.execute(
            "INSERT INTO acceptance_reports(id,run_id,project_id,candidate_ref,baseline_ref,"
            "test_manifest_json,policy_json,stats_json,decision,consumed,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?, '1', ?)",
            (rep_id, rid, run["project_id"], cand_pv_id, baseline_pv_id,
             json.dumps({"sealed_items": [p["item_id"] for p in pairs]}, ensure_ascii=False),
             json.dumps(policy, ensure_ascii=False), json.dumps(stats, ensure_ascii=False),
             decision, now_iso()))
        RunService(self.db)._audit("acceptance.complete", rep_id, canonical_hash(stats))
        with self.db.tx() as conn:
            RunService(self.db)._emit(conn, rid, None, "acceptance_done",
                                      {"report_id": rep_id, "decision": decision})
        return self.get(rep_id)

    def get(self, rep_id: str) -> dict:
        r = self.db.one("SELECT * FROM acceptance_reports WHERE id=?", (rep_id,))
        if r is None:
            raise BizError("NOT_FOUND", "报告不存在", status=404)
        return {"id": r["id"], "run_id": r["run_id"], "project_id": r["project_id"],
                "candidate_ref": r["candidate_ref"], "baseline_ref": r["baseline_ref"],
                "decision": r["decision"], "consumed": bool(r["consumed"]),
                "stats": jloads(r["stats_json"], {}), "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query(
            "SELECT id FROM acceptance_reports WHERE project_id=? ORDER BY created_at DESC", (pid,))]


# ---------------------------------------------------------------- 使用版本与反馈 P13

class ReleaseService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def adopt(self, pid: str, prompt_version_id: str, report_ref: str, mode: str = "active",
             expected_revision: int | None = None, model_config: dict | None = None) -> dict:
        ProjectsService(self.db).ensure_active(pid)
        pv = self.db.one("SELECT 1 FROM prompt_versions WHERE id=? AND project_id=?",
                         (prompt_version_id, pid))
        if pv is None:
            raise BizError("NOT_FOUND", "提示词版本不属于该项目", status=404)
        model_config = model_config or {"connection_id": "conn_mock", "model": "mock-gen-1"}
        if mode == "active":
            # 正式采用必须绑定verified报告（TC048）
            if not report_ref:
                raise BizError("REPORT_REQUIRED", "正式采用必须绑定验收报告（TC048）", status=422)
            rep = self.db.one("SELECT * FROM acceptance_reports WHERE id=? AND project_id=?",
                              (report_ref, pid))
            if rep is None:
                raise BizError("NOT_FOUND", "验收报告不存在", status=404)
            if rep["decision"] != "verified_improvement":
                raise BizError("REPORT_NOT_VERIFIED",
                               f"报告结论为 {rep['decision']}，未验证的候选只能trial（TC048）", status=422)
            if rep["candidate_ref"] != prompt_version_id:
                raise BizError("REPORT_MISMATCH", "报告对应的候选与发布版本不一致")
        current = self.db.one("SELECT * FROM releases WHERE project_id=? AND status='active'", (pid,))
        if current is not None and expected_revision is not None \
                and current["revision"] != expected_revision:
            raise BizError("REVISION_CONFLICT", "已有并发发布：请刷新后重试（TC049）", status=409)
        rid = new_id("rel")
        with self.db.tx() as conn:
            if mode == "active":
                if current is not None:
                    conn.execute("UPDATE releases SET status='superseded' WHERE id=?", (current["id"],))
                conn.execute(
                    "INSERT INTO releases(id,project_id,prompt_version_id,model_config_json,"
                    "report_ref,status,revision,created_at) VALUES(?,?,?,?,?, 'active', 1, ?)",
                    (rid, pid, prompt_version_id, json.dumps(model_config, ensure_ascii=False),
                     report_ref or "", now_iso()))
            else:
                conn.execute(
                    "INSERT INTO releases(id,project_id,prompt_version_id,model_config_json,"
                    "report_ref,status,revision,created_at) VALUES(?,?,?,?,?, 'trial', 1, ?)",
                    (rid, pid, prompt_version_id, json.dumps(model_config, ensure_ascii=False),
                     report_ref or "", now_iso()))
        return self.get(rid)

    def get(self, rid: str) -> dict:
        r = self.db.one("SELECT * FROM releases WHERE id=?", (rid,))
        if r is None:
            raise BizError("NOT_FOUND", "发布记录不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"],
                "prompt_version_id": r["prompt_version_id"],
                "model_config": jloads(r["model_config_json"], {}), "report_ref": r["report_ref"],
                "status": r["status"], "revision": r["revision"], "created_at": r["created_at"]}

    def current(self, pid: str) -> dict | None:
        r = self.db.one("SELECT * FROM releases WHERE project_id=? AND status='active'", (pid,))
        return self.get(r["id"]) if r else None

    def history(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query(
            "SELECT id FROM releases WHERE project_id=? ORDER BY created_at DESC", (pid,))]

    def rollback(self, release_id: str, target_release_id: str) -> dict:
        """回滚：产生新的active发布，恢复文本+模型+模板；不删除历史（TC049）。"""
        rel = self.get(release_id)
        target = self.get(target_release_id)
        if rel["project_id"] != target["project_id"]:
            raise BizError("NOT_FOUND", "回滚目标不属于同一项目", status=404)
        rid = new_id("rel")
        with self.db.tx() as conn:
            conn.execute("UPDATE releases SET status='rolled_back' WHERE id=?", (release_id,))
            conn.execute(
                "INSERT INTO releases(id,project_id,prompt_version_id,model_config_json,"
                "report_ref,status,revision,created_at)"
                " VALUES(?,?,?,?,?, 'active', 1, ?)",
                (rid, target["project_id"], target["prompt_version_id"],
                 json.dumps(target["model_config"], ensure_ascii=False),
                 target["report_ref"], now_iso()))
        self.get_db_audit("release.rollback", rid)
        return self.get(rid)

    def get_db_audit(self, action, target):
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", action, target, "", now_iso()))

    def submit_feedback(self, release_id: str, adoption: str, edit_time: str = "",
                        reason: str = "") -> dict:
        rel = self.get(release_id)
        if adoption not in ("direct", "minor_edit", "major_edit", "abandoned"):
            raise BizError("ADOPTION_INVALID", "采用状态必须是 direct/minor_edit/major_edit/abandoned")
        fid = new_id("fb")
        self.db.execute(
            "INSERT INTO feedback(id,release_id,project_id,adoption,edit_time,reason,status,"
            "created_at) VALUES(?,?,?,?,?,?, 'pending_review', ?)",
            (fid, release_id, rel["project_id"], adoption, edit_time, reason, now_iso()))
        # 反馈回流进入待核验池，不自动成为测试gold（TC050）
        return {"id": fid, "status": "pending_review",
                "note": "反馈进入待核验开发案例池，需人工核验后才能进入数据集"}

    def feedback_list(self, pid: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM feedback WHERE project_id=? ORDER BY created_at DESC",
                             (pid,))
        return [{"id": r["id"], "release_id": r["release_id"], "adoption": r["adoption"],
                 "edit_time": r["edit_time"], "reason": r["reason"], "status": r["status"],
                 "created_at": r["created_at"]} for r in rows]

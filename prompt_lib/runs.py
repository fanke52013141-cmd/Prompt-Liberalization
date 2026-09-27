"""运行服务：快照、幂等、执行、取消、候选锁定、独立验收、发布与反馈（D06/D09/D10）。"""
from __future__ import annotations

import json
import threading

from .core import BizError, canonical_hash, new_id, now_iso
from .db import DB, get_db
from .domain import DataService, FeedbackService, ProjectsService, jloads
from .engine import decide_keep, generate_once, propose_revision, score_prompt
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

    def get(self, rid: str, detail: bool = False) -> dict:
        r = self.db.one("SELECT * FROM runs WHERE id=?", (rid,))
        if r is None:
            raise BizError("NOT_FOUND", "运行不存在", status=404)
        rounds = [{"id": x["id"], "round_no": x["round_no"], "hypothesis": x["hypothesis"],
                   "problem_evidence": jloads(x["problem_evidence_json"], []),
                   "prompt_version_id": x["prompt_version_id"], "score": x["score"],
                   "prev_score": x["prev_score"], "usable_rate": x["usable_rate"],
                   "severe": x["severe"], "regressions": x["regressions"],
                   "fixed_problems": jloads(x["fixed_problems_json"], []),
                   "decision": x["decision"], "rationale": x["rationale"],
                   "next_direction": x["next_direction"], "length_chars": x["length_chars"],
                   "status": x["status"], "detail": jloads(x["detail_json"], {}),
                   "created_at": x["created_at"]}
                  for x in self.db.query(
                      "SELECT * FROM run_rounds WHERE run_id=? ORDER BY round_no", (rid,))]
        base_detail = jloads(r["baseline_detail_json"], {})
        out = {"id": r["id"], "project_id": r["project_id"], "state": r["state"],
               "stage": r["stage"], "stop_reason": r["stop_reason"], "revision": r["revision"],
               "snapshot": jloads(r["snapshot_json"], {}), "snapshot_hash": r["snapshot_hash"],
               "baseline_prompt_id": r["baseline_prompt_id"],
               "baseline_score": r["baseline_score"],
               "candidates": jloads(r["candidates_json"], []),
               "locked_candidate": r["locked_candidate"],
               "round_no": r["round_no"], "current_best_pv": r["current_best_pv"],
               "stall_count": r["stall_count"],
               "baseline_problems": base_detail.get("problems", {}),
               "rounds": rounds,
               "budget": jloads(r["budget_state_json"], {}),
               "error": r["error"], "created_at": r["created_at"]}
        if detail:
            out["baseline_detail"] = base_detail
        return out

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in
                self.db.query("SELECT id FROM runs WHERE project_id=? ORDER BY created_at DESC",
                              (pid,))]

    # ---------------- 执行（后台线程；协作式取消 TC038；预算暂停）
    def start(self, rid: str) -> None:
        run = self.get(rid)
        if run["state"] not in ("queued", "paused_budget", "waiting_human"):
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
        """多轮优化编排（优化1.0 §8）：原始测评 → 逐轮“证据→改写→复测→择保留”。

        - 未知总收益不展示虚假进度；每轮交付问题证据/假设/测评变化/保留决定
        - 底线：原有正确案例回退与严重错误增加会否决平均分提升（decide_keep）
        - 人工参与模式：每轮完成后进入 waiting_human，等待显式继续
        - 离开页面不取消运行；停止/失败保留已完成输出
        """
        from .domain import PromptService
        run = self.get(rid)
        snapshot = run["snapshot"]
        budget = BudgetState(run["budget"])
        ledger = Ledger(self.db)
        project = ProjectsService(self.db).get(run["project_id"])
        try:
            opt = snapshot.get("optimization", {})
            max_rounds = int(opt.get("max_rounds") or opt.get("max_candidates") or 0)
            stall_limit = max(1, int(opt.get("stall_rounds") or 2))
            length_limit = int(opt.get("length_limit_chars") or 4000)
            human_in_loop = bool(opt.get("human_in_loop"))
            target = opt.get("target_score")
            min_delta = float(opt.get("min_delta") or 0.0)
            dev_ids = snapshot["data"]["dev_item_ids"]
            sample = dev_ids[: int(opt.get("dev_sample_size") or len(dev_ids))]
            prompt_svc = PromptService(self.db)
            fb_svc = FeedbackService(self.db)
            fb_by_item: dict[str, list[dict]] = {}
            for f in fb_svc.list(run["project_id"]):
                if f["item_id"] and f["status"] not in ("resolved", "retired"):
                    fb_by_item.setdefault(f["item_id"], []).append(f)
            models = snapshot["models"]

            baseline_detail = run.get("baseline_detail") or {}
            if run["round_no"] == 0 and not baseline_detail:
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "stage", {"stage": "baseline"})
                self._set_state(rid, "running", "baseline")
                pv = self.db.one("SELECT * FROM prompt_versions WHERE id=?",
                                 (snapshot["prompt"]["baseline_id"],))
                baseline_pv = prompt_svc._row(pv)
                base_res = score_prompt(self.db, project, baseline_pv, sample,
                                        snapshot["rubric_id"], models, rid,
                                        "search", budget, ledger)
                problems = self._check_item_problems(
                    project, baseline_pv, base_res["items"], fb_by_item,
                    snapshot["rubric_id"], models, rid, "search", budget, ledger)
                baseline_detail = {"items": base_res["items"], "score": base_res["score"],
                                   "usable_rate": base_res["usable_rate"],
                                   "severe": base_res["severe"], "n": base_res["n"],
                                   "problems": problems}
                self.db.execute("UPDATE runs SET baseline_score=?, baseline_detail_json=?,"
                                " current_best_pv=?, best_detail_json=?, updated_at=? WHERE id=?",
                                (base_res["score"],
                                 json.dumps(baseline_detail, ensure_ascii=False),
                                 baseline_pv["id"],
                                 json.dumps({"items": base_res["items"],
                                             "score": base_res["score"],
                                             "usable_rate": base_res["usable_rate"],
                                             "severe": base_res["severe"],
                                             "problems": problems},
                                            ensure_ascii=False),
                                now_iso(), rid))
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "baseline_done",
                               {k: base_res[k] for k in
                                ("score", "usable_rate", "n", "n_scored", "severe")})
                    problem_list = []
                    for iid, per in problems.items():
                        for f in fb_by_item.get(iid, []):
                            problem_list.append({"item_id": iid, "case_id": per.get("case_id"),
                                                 "feedback_id": f["id"], "problem": f["problem"],
                                                 "severity": f["severity"], "status": f["status"],
                                                 "tags": f["tags"],
                                                 "check": per["statuses"].get(f["id"])})
                    self._emit(conn, rid, None, "problems_identified",
                               {"problems": problem_list,
                                "note": "以上为原始测评发现的问题起点；无登记专家意见时按评价标准诊断"})

            current_best_id = run["current_best_pv"] or snapshot["prompt"]["baseline_id"]

            candidates: list[dict] = jloads(
                self.db.one("SELECT candidates_json FROM runs WHERE id=?" , (rid,))["candidates_json"],
                [])
            stall = int(run["stall_count"])
            stop_reason = ""
            round_i = int(run["round_no"])

            while round_i < max_rounds:
                round_i += 1
                if self._cancelled(rid):
                    raise BizError("CANCELLED", "运行已取消：停止派发新请求，在途结果仍入账（TC038）")
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "stage", {"stage": f"round_{round_i}"})
                self._set_state(rid, "running", f"round_{round_i}")
                best_row = self.db.one("SELECT * FROM prompt_versions WHERE id=?",
                                       (current_best_id,))
                best_pv = prompt_svc._row(best_row)
                best_detail = jloads(self.db.one(
                    "SELECT best_detail_json FROM runs WHERE id=?", (rid,))["best_detail_json"], {})
                failures, correct = self._build_evidence(best_detail, fb_by_item)
                history = [{"round": rr["round_no"], "decision": rr["decision"],
                            "hypothesis": rr["hypothesis"], "result": rr["status"],
                            "rationale": rr["rationale"]}
                           for rr in self.db.query(
                               "SELECT * FROM run_rounds WHERE run_id=? ORDER BY round_no", (rid,))]
                rev = propose_revision(self.db, models.get("optimizer") or models["generation"],
                                       best_pv, project,
                                       self._rubric_schema(snapshot["rubric_id"]),
                                       {"failures": failures, "correct": correct},
                                       history, rid, budget, ledger, length_limit)
                if not rev["ok"]:
                    stall += 1
                    self._record_round(rid, round_i, status="rewrite_failed",
                                       rationale=rev["reason"])
                    with self.db.tx() as conn:
                        self._emit(conn, rid, None, "rewrite_failed",
                                   {"round": round_i, "reason": rev["reason"]})
                    self._persist_progress(rid, round_i, current_best_id, stall)
                    if human_in_loop and stall < stall_limit:
                        self._pause_for_human(rid, round_i, "改写失败后等待人工确认")
                        return
                    if stall >= stall_limit:
                        stop_reason = "rewrite_stalled"
                        break
                    continue
                if rev["new_body"].strip() == best_pv["body"].strip():
                    stall += 1
                    self._record_round(rid, round_i, status="no_change",
                                       hypothesis=rev["hypothesis"],
                                       rationale="优化器未提出有依据的修改（原样返回）")
                    with self.db.tx() as conn:
                        self._emit(conn, rid, None, "no_change",
                                   {"round": round_i, "hypothesis": rev["hypothesis"]})
                    self._persist_progress(rid, round_i, current_best_id, stall)
                    if human_in_loop and stall < stall_limit:
                        self._pause_for_human(rid, round_i, "无修改建议，等待人工确认")
                        return
                    if stall >= stall_limit:
                        stop_reason = "stalled_no_gain"
                        break
                    continue

                cand_pv = prompt_svc.create_version(
                    run["project_id"], best_pv["name"], rev["new_body"],
                    best_pv["frozen_segments"], best_pv["variables"], best_pv["params"],
                    parent_id=best_pv["id"], origin="optimizer",
                    hypothesis=rev["hypothesis"])
                prompt_svc.validate_candidate(best_pv, cand_pv)  # 冻结段校验：任何收费请求前（TC027）
                cand_res = score_prompt(self.db, project, cand_pv, sample,
                                        snapshot["rubric_id"], models, rid, "search",
                                        budget, ledger)
                cand_problems = self._check_item_problems(
                    project, cand_pv, cand_res["items"], fb_by_item,
                    snapshot["rubric_id"], models, rid, "search", budget, ledger)
                prev_usable = {it["item_id"]: it.get("usable") for it in best_detail.get("items", [])}
                regressions = sum(1 for it in cand_res["items"]
                                  if prev_usable.get(it["item_id"]) is True
                                  and it.get("usable") is False)
                base_problems = best_detail.get("problems", {})
                fixed_ids, open_before = [], 0
                for iid, per in cand_problems.items():
                    for fbk in per["items"]:
                        fid = fbk["feedback_id"]
                        base_st = base_problems.get(iid, {}).get("statuses", {}).get(fid)
                        cand_st = fbk["status"]
                        if base_st in (None, "resolved"):
                            continue
                        open_before += 1
                        if cand_st == "resolved":
                            fixed_ids.append(fid)
                decision = decide_keep(
                    {"score": best_detail.get("score", 0.0), "severe": best_detail.get("severe", 0),
                     "length": len(best_pv["body"])},
                    {"score": cand_res["score"], "severe": cand_res["severe"],
                     "length": cand_res["length"], "regressions": regressions,
                     "fixed_problems": len(fixed_ids), "open_problems_before": open_before},
                    min_delta)
                rationale_extra = ""
                if decision["decision"] == "discarded" and regressions == 0 and \
                        cand_res["usable_rate"] > (best_detail.get("usable_rate") or 0) and \
                        len(candidates) < 4:
                    decision = dict(decision, decision="retained_alt")
                    rationale_extra = "（虽未成为最优，但可用率更优且无回退：作为各有优势的备选保留，供按偏好选择）"
                cand_entry = {
                    "candidate_id": f"cand_{round_i}", "round": round_i,
                    "prompt_version_id": cand_pv["id"], "hash": cand_pv["hash"],
                    "parent_id": best_pv["id"],
                    "score": cand_res["score"], "usable_rate": cand_res["usable_rate"],
                    "severe": cand_res["severe"], "regressions": regressions,
                    "fixed_problems": fixed_ids, "length": cand_res["length"],
                    "hypothesis": rev["hypothesis"], "change_summary": rev.get("change_summary", ""),
                    "decision": decision["decision"], "rationale": decision["rationale"] + rationale_extra,
                    "evidence": cand_res["failures"][:5]}
                candidates.append(cand_entry)
                self._record_round(rid, round_i, status="scored", hypothesis=rev["hypothesis"],
                                   problem_evidence=[{"case_id": f.get("case"), "dims": f.get("dims")}
                                                     for f in failures[:5]],
                                   prompt_version_id=cand_pv["id"], score=cand_res["score"],
                                   prev_score=best_detail.get("score", 0.0),
                                   usable_rate=cand_res["usable_rate"], severe=cand_res["severe"],
                                   regressions=regressions, fixed_problems=fixed_ids,
                                   decision=decision["decision"],
                                   rationale=decision["rationale"] + rationale_extra,
                                   length_chars=cand_res["length"],
                                   detail={"change_summary": rev.get("change_summary", ""),
                                           "problems": cand_problems})
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "candidate_done",
                               {"candidate_id": cand_entry["candidate_id"],
                                "score": cand_res["score"],
                                "decision": decision["decision"],
                                "rationale": cand_entry["rationale"]})
                if decision["decision"] == "kept":
                    stall = 0
                    current_best_id = cand_pv["id"]
                    self._persist_progress(rid, round_i, cand_pv["id"], stall,
                                           best_detail={"items": cand_res["items"],
                                                        "score": cand_res["score"],
                                                        "usable_rate": cand_res["usable_rate"],
                                                        "severe": cand_res["severe"],
                                                        "problems": cand_problems},
                                           candidates=candidates)
                else:
                    stall += 1
                    self._persist_progress(rid, round_i, current_best_id, stall,
                                           candidates=candidates)
                if target is not None and cand_res["score"] >= float(target) and \
                        decision["decision"] == "kept":
                    stop_reason = "target_reached"
                    break
                if stall >= stall_limit:
                    stop_reason = "stalled_no_gain"
                    break
                if human_in_loop:
                    self._pause_for_human(rid, round_i, "本轮已完成，等待指定评价后继续")
                    return

            baseline_score = jloads(self.db.one(
                "SELECT baseline_detail_json FROM runs WHERE id=?",
                (rid,))["baseline_detail_json"], {}).get("score", 0.0)
            self.db.execute("UPDATE runs SET candidates_json=?, baseline_score=?, updated_at=?"
                            " WHERE id=?",
                            (json.dumps(candidates, ensure_ascii=False), baseline_score,
                             now_iso(), rid))
            best_score = jloads(self.db.one("SELECT best_detail_json FROM runs WHERE id=?",
                                            (rid,))["best_detail_json"], {}).get("score")
            if self._cancelled(rid):
                final_reason = "user_cancelled"
                self._set_state(rid, "cancelled", stop_reason=final_reason)
            else:
                final_reason = stop_reason or (
                    "candidate_found" if current_best_id != snapshot["prompt"]["baseline_id"]
                    else "no_improvement")
                self._set_state(rid, "completed", "done", final_reason)
            with self.db.tx() as conn:
                self._emit(conn, rid, None, "completed",
                           {"stop_reason": final_reason,
                            "baseline_score": baseline_score,
                            "best_score": best_score,
                            "note": "无提升为合法结果：保留基线（TC037）"
                            if final_reason == "no_improvement" else ""})
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

    # ---- 编排辅助 ----
    def _rubric_schema(self, rubric_id: str) -> dict:
        r = self.db.one("SELECT schema_json FROM rubrics WHERE id=?", (rubric_id,))
        return json.loads(r["schema_json"]) if r else {}

    def _build_evidence(self, best_detail: dict, fb_by_item: dict) -> tuple[list[dict], list[dict]]:
        """组装优化器证据（§8.2）：失败案例带完整输入/输出/专家意见；正确案例要求保持。"""
        items = best_detail.get("items", [])
        scored = [it for it in items if it.get("score") is not None]
        failures = sorted(scored, key=lambda it: (it.get("score") or 0))[:5]
        correct = [it for it in scored if it.get("usable") and not it.get("severe")][:3]
        def pack(it):
            fb_rows = fb_by_item.get(it["item_id"], [])
            return {"case_id": it["case_id"], "item_id": it["item_id"],
                    "runtime_input": it.get("runtime_input", {}),
                    "output_text": it.get("output_text", ""), "dims": it.get("dims", {}),
                    "feedback": [{"problem": f["problem"], "quote": f["quote"],
                                  "expected": f["expected"], "severity": f["severity"],
                                  "status": f["status"], "tags": f["tags"],
                                  "remark": f["remark"]} for f in fb_rows]}
        return [pack(it) for it in failures], [pack(it) for it in correct]

    def _check_item_problems(self, project, prompt_pv, items_detail, fb_by_item,
                             rubric_id, models, rid, phase, budget, ledger) -> dict:
        """对带登记专家意见的案例核查问题状态；无意见的案例不发起调用。"""
        from .engine import check_problems
        out = {}
        ev_model = models.get("evaluation") or models["generation"]
        for it in items_detail:
            fbs = fb_by_item.get(it["item_id"])
            if not fbs or it.get("gen_status") != "ok" or not it.get("output_text"):
                continue
            plist = [{"id": f["id"], "problem": f["problem"], "expected": f["expected"]}
                     for f in fbs]
            res = check_problems(self.db, plist, it["output_text"], ev_model, rid,
                                 phase, budget, ledger)
            out[it["item_id"]] = {"case_id": it["case_id"], "abstain": res.get("abstain", False),
                                  "statuses": res["statuses"],
                                  "items": [{"feedback_id": p["id"], "status": res["statuses"][p["id"]]}
                                            for p in plist]}
        return out

    def _record_round(self, rid: str, round_no: int, status: str, hypothesis: str = "",
                      problem_evidence: list | None = None, prompt_version_id: str = "",
                      score: float | None = None, prev_score: float | None = None,
                      usable_rate: float | None = None, severe: int | None = None,
                      regressions: int = 0, fixed_problems: list | None = None,
                      decision: str = "", rationale: str = "", next_direction: str = "",
                      length_chars: int = 0, detail: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO run_rounds(id,run_id,round_no,hypothesis,"
            "problem_evidence_json,prompt_version_id,score,prev_score,usable_rate,severe,"
            "regressions,fixed_problems_json,decision,rationale,next_direction,length_chars,"
            "status,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rnd_{rid}_{round_no}", rid, round_no, hypothesis,
             json.dumps(problem_evidence or [], ensure_ascii=False), prompt_version_id,
             score, prev_score, usable_rate, severe, regressions,
             json.dumps(fixed_problems or [], ensure_ascii=False), decision, rationale,
             next_direction, length_chars, status,
             json.dumps(detail or {}, ensure_ascii=False), now_iso()))

    def _persist_progress(self, rid: str, round_no: int, current_best_pv: str, stall: int,
                          best_detail: dict | None = None, candidates: list | None = None) -> None:
        sets = ["round_no=?", "current_best_pv=?", "stall_count=?", "updated_at=?"]
        params = [round_no, current_best_pv, stall, now_iso()]
        if best_detail is not None:
            sets.append("best_detail_json=?")
            params.append(json.dumps(best_detail, ensure_ascii=False))
        if candidates is not None:
            sets.append("candidates_json=?")
            params.append(json.dumps(candidates, ensure_ascii=False))
        params.append(rid)
        self.db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", tuple(params))

    def _pause_for_human(self, rid: str, round_no: int, reason: str) -> None:
        self._set_state(rid, "waiting_human", f"round_{round_no}")
        with self.db.tx() as conn:
            self._emit(conn, rid, None, "waiting_human",
                       {"round": round_no, "reason": reason,
                        "note": "运行已暂停等待人工参与；离开页面不影响状态，服务保持运行时后台等待"})

    def continue_run(self, rid: str) -> dict:
        """人工参与模式：等待人工评价后显式继续（§8.5）。"""
        run = self.get(rid)
        if run["state"] != "waiting_human":
            raise BizError("RUN_STATE_INVALID", "只有等待人工参与的运行可以继续", status=409)
        self.start(rid)
        return self.get(rid)

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
        fb_by_item: dict[str, list[dict]] = {}
        for f in FeedbackService(self.db).list(run["project_id"]):
            if f["item_id"] and f["status"] not in ("resolved", "retired"):
                fb_by_item.setdefault(f["item_id"], []).append(f)
        from .engine import check_problems, evaluate_once
        from .providers import has_severe_error, is_usable
        for art in sealed:
            item = {"id": art["item_id"], "case_id": art["item_id"],
                    "runtime_input": art["runtime_input"], "source_group_id": art["item_id"]}
            # 交错执行：基线与候选同输入/同参数/同模型（TC042）
            ob = generate_once(self.db, project, pv_base, item, models["generation"], rid,
                               "acceptance", budget, ledger)
            oc = generate_once(self.db, project, pv_cand, item, models["generation"], rid,
                               "acceptance", budget, ledger)
            row_b = self.db.one("SELECT text FROM outputs WHERE id=?", (ob["id"],))
            row_c = self.db.one("SELECT text FROM outputs WHERE id=?", (oc["id"],))
            sb = evaluate_once(row_b["text"], rubric_id, models["evaluation"], rid, "acceptance",
                               budget=budget, ledger=ledger) \
                if ob["status"] == "ok" else {"abstain": True}
            sc = evaluate_once(row_c["text"], rubric_id, models["evaluation"], rid, "acceptance",
                               budget=budget, ledger=ledger) \
                if oc["status"] == "ok" else {"abstain": True}
            entry = {"item_id": art["item_id"], "base_ok": ob["status"] == "ok",
                     "cand_ok": oc["status"] == "ok",
                     "base_usable": is_usable(sb) if not sb.get("abstain") else None,
                     "cand_usable": is_usable(sc) if not sc.get("abstain") else None,
                     "base_severe": has_severe_error(row_b["text"], sb) if not sb.get("abstain")
                     and ob["status"] == "ok" else None,
                     "cand_severe": has_severe_error(row_c["text"], sc) if not sc.get("abstain")
                     and oc["status"] == "ok" else None}
            # 问题级核查（§9.3 问题项口径）：仅对登记了专家意见的封存案例发起调用
            fbs = fb_by_item.get(art["item_id"]) or []
            if fbs and ob["status"] == "ok" and oc["status"] == "ok":
                plist = [{"id": f["id"], "problem": f["problem"], "expected": f["expected"]}
                         for f in fbs]
                entry["base_problem_status"] = check_problems(
                    self.db, plist, row_b["text"], models["evaluation"], rid,
                    "acceptance", budget, ledger)["statuses"]
                entry["cand_problem_status"] = check_problems(
                    self.db, plist, row_c["text"], models["evaluation"], rid,
                    "acceptance", budget, ledger)["statuses"]
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
        # 问题项口径（§9.3）：完全解决/部分解决/未解决/无法判断 分开统计；
        # ABCD 分布同时展示原问题改善与新增问题两个维度，无法判断单独记录
        resolution = {"resolved": 0, "partial": 0, "unresolved": 0, "unknown": 0,
                      "already_ok": 0}
        abcd = {"A": 0, "B": 0, "C": 0, "D": 0, "cannot_judge": 0, "not_rated": 0}
        rated_cases = 0
        for p in pairs:
            base_ps = p.get("base_problem_status") or {}
            cand_ps = p.get("cand_problem_status") or {}
            if not base_ps:
                abcd["not_rated"] += 1  # 无登记问题：按通用质量标准评价，不强行套用原问题解决率
                continue
            rated_cases += 1
            present = [k for k, st in base_ps.items() if st in ("partial", "unresolved")]
            cand_sts = [cand_ps.get(k, "unknown") for k in present]
            for k, st in base_ps.items():
                cst = cand_ps.get(k, "unknown")
                if st == "resolved":
                    resolution["already_ok"] += 1
                else:
                    resolution[cst if cst in resolution else "unknown"] += 1
            if p.get("cand_severe") and not p.get("base_severe"):
                abcd["D"] += 1
            elif any(s == "unknown" for s in cand_sts):
                abcd["cannot_judge"] += 1
            elif not present:
                abcd["not_rated"] += 1  # 基线即无此问题：无原问题可解决
            elif present and all(s == "resolved" for s in cand_sts):
                abcd["A"] += 1
            elif present and all(s == "unresolved" for s in cand_sts):
                abcd["C"] += 1
            else:
                abcd["B"] += 1
        total_registered = sum(v for k, v in resolution.items() if k != "already_ok") \
            + resolution["already_ok"]

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
            "problem_stats": {
                "total_registered": total_registered, "rated_cases": rated_cases,
                "resolution": resolution, "abcd": abcd,
                "note": ("封存案例未登记专家问题：按通用质量标准评价，不强行套用原问题解决率（§9.2）"
                         if total_registered == 0 else
                         "ABCD按默认规则建议生成，始终同时展示原问题改善与新增问题；无法判断单独记录"),
            },
            "evidence_scope": {
                "optimization_sample": {"n": len(snapshot.get("data", {}).get("dev_item_ids", [])),
                                        "role": "本批案例改善（不构成独立证明）"},
                "independent": {"set": "sealed_test", "n": len(sealed), "consumed": True,
                                "role": "独立验证（未参与修改的案例）"},
                "note": "优化样例与独立验证结果分别展示；复制文本不等于验证通过（§9.2/§9.4）",
            },
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

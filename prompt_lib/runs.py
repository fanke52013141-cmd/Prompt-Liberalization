"""运行服务：快照、幂等、执行、取消、候选锁定、独立验收、发布与反馈（D06/D09/D10）。"""
from __future__ import annotations

import json
import random
import threading
from contextlib import nullcontext
from pathlib import Path

from .core import BizError, canonical_hash, new_id, now_iso
from .db import DB, get_db
from .domain import DataService, FeedbackService, ProjectsService, jloads
from .engine import decide_keep, generate_once, propose_revision, score_prompt
from .experiment_data import freeze_cases, verify_cases
from .ledger import BudgetState, Ledger, validate_money_budget_needs_prices
from .stats import (exact_mcnemar, group_outcomes, missing_bounds, paired_bootstrap,
                    zero_event_upper_bound)
from prompt_core.decision import adoption_decision
from prompt_core.statistics import summarize_pairs

_engine_threads: dict[str, threading.Thread] = {}
_acceptance_locks: dict[tuple[str, str], threading.Lock] = {}


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
        if (draft.get("acceptance") or {}).get("evaluation_source", "model") not in ("model", "human"):
            errors["acceptance.evaluation_source"] = "最终评价来源必须为model或human"

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
                jr = self.db.one("SELECT status,metrics_json FROM judges WHERE id=?", (judge_id,))
                if jr["status"] == "stale":
                    errors["judge_id"] = "评价器已stale：标准或模型变化后需重新校准（TC024）"
                elif jr["status"] != "audited" and mode == "batch":
                    errors["judge_id"] = "批量模式需要已审计(audited)的评价器；可改用explore模式"
                elif mode == "batch" and jloads(jr["metrics_json"], {}).get("admission", {}).get("eligible") is not True:
                    errors["judge_id"] = "评价器虽已审计，但未通过事先配置的校准门槛；请校准或改用探索"
        elif mode == "batch":
            errors["judge_id"] = "批量模式需要评价器；或改用explore模式（允许退回人工评价）"
        models = draft.get("models") or {}
        if mode == "batch" and judge_id and not errors.get("judge_id"):
            from .judge_binding import admitted_for
            jr = self.db.one("SELECT * FROM judges WHERE id=?", (judge_id,))
            if not admitted_for(self.db, jr, draft.get("rubric_id"), models.get("evaluation") or {}):
                errors["judge_id"] = "校准未绑定当前评价标准、模型、参数及评价程序；请重新校准或改用探索"
        metric = (models.get("evaluation") or {}).get("metric")
        if metric:
            from prompt_core.metrics import validate_metric_config
            try:
                validate_metric_config(metric)
                if not isinstance(metric.get("reference_field"), str) or not metric["reference_field"]:
                    raise ValueError("须指定评价参考字段")
                rubric = self.db.one("SELECT schema_json FROM rubrics WHERE id=?", (draft.get("rubric_id"),))
                if rubric and len(jloads(rubric["schema_json"], {}).get("dimensions", [])) != 1:
                    raise ValueError("确定性指标须对应单一明确评分维度")
            except ValueError as exc:
                errors["models.evaluation.metric"] = str(exc)
        for role in ("generation", "evaluation", "optimizer"):
            mc = models.get(role)
            if not mc or not mc.get("connection_id"):
                errors[f"models.{role}"] = "必须配置模型连接（可使用内置离线模拟供应商）"
        # 预算类配置错误直接抛出，保留具体错误码（TC032/TC033）
        budget = draft.get("budget") or {}
        b = BudgetState(budget)
        b.validate()
        connections = {c["id"]: c for c in _settings("connections", [])}
        resolved_models = {role: {**config, "model": config.get("model") or
                                  connections.get(config.get("connection_id"), {}).get("model") or "default"}
                           for role, config in models.items()}
        prices = _settings("prices", {})
        validate_money_budget_needs_prices(budget, resolved_models, prices)
        if b.mode == "money":
            b.prices = {config["model"]: dict(prices[config["model"]]) for config in resolved_models.values()}
        normalized["budget"] = b.to_json()
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
                "SELECT id FROM dataset_items WHERE project_id=? AND split='dev'", (pid,))}
            bad = [i for i in dev_ids if i not in frozen]
            if bad:
                errors["data.dev_item_ids"] = f"以下案例不在已冻结的开发集合中；选择案例不能进入改写证据：{bad[:5]}"
        if len(set(dev_ids)) != len(dev_ids):
            errors["data.dev_item_ids"] = "开发案例编号不能重复"
        select_ids = (draft.get("data") or {}).get("select_item_ids") or []
        if not isinstance(select_ids, list) or any(not isinstance(i, str) for i in select_ids):
            errors["data.select_item_ids"] = "选择案例必须是明确编号列表"
        elif select_ids:
            available = {r["id"] for r in self.db.query("SELECT id FROM dataset_items WHERE project_id=? AND split='select'", (pid,))}
            if len(set(select_ids)) != len(select_ids) or any(i not in available for i in select_ids):
                errors["data.select_item_ids"] = "选择案例须去重且属于当前项目的冻结选择集合"
        if mode == "batch" and not select_ids:
            errors["data.select_item_ids"] = "自动批量搜索需要独立选择案例，探索模式可暂不配置"
        optimization = dict(draft.get("optimization") or {})
        strategy = optimization.get("strategy", "reflection")
        if strategy not in ("single", "reflection", "gepa"):
            errors["optimization.strategy"] = "优化策略必须是single、reflection或gepa"
        if strategy == "single":
            optimization["max_rounds"] = 1
        if strategy == "gepa":
            metric_calls = optimization.get("gepa_max_metric_calls", 40)
            if (isinstance(metric_calls, bool) or not isinstance(metric_calls, int)
                    or not 1 <= metric_calls <= 10000):
                errors["optimization.gepa_max_metric_calls"] = "GEPA评价调用上限必须是1到10000的整数"
            if not select_ids:
                errors["data.select_item_ids"] = "GEPA需要冻结的独立选择案例；请先准备select集合"
            if optimization.get("human_in_loop"):
                errors["optimization.human_in_loop"] = "GEPA运行目前不支持逐轮人工暂停；请关闭人工参与模式"
            elif (manifest is not None and isinstance(dev_ids, list)
                  and isinstance(select_ids, list)):
                ids = list(dict.fromkeys(dev_ids + select_ids))
                rows = self.db.query(
                    "SELECT id,source_group_id FROM dataset_items WHERE project_id=? AND id IN (" +
                    ",".join("?" for _ in ids) + ")", (pid, *ids)) if ids else []
                group_by_id = {row["id"]: row["source_group_id"] for row in rows}
                dev_groups = {group_by_id.get(item_id) for item_id in dev_ids}
                select_groups = {group_by_id.get(item_id) for item_id in select_ids}
                overlap = sorted(group for group in dev_groups & select_groups if group)
                if overlap:
                    errors["data.select_item_ids"] = "GEPA开发与选择案例必须来自互不重叠的来源组"
            try:
                from prompt_gepa.adapter import load_sdk
                load_sdk()
            except (RuntimeError, ImportError) as exc:
                errors["optimization.strategy"] = str(exc)
        if errors:
            raise BizError("SNAPSHOT_INVALID", "实验配置存在缺项，请按字段定位修正",
                           field_errors=errors, status=422)
        normalized["project_id"] = pid
        data = dict(draft.get("data") or {})
        sample_size = int((draft.get("optimization") or {}).get("dev_sample_size") or len(dev_ids))
        if sample_size <= 0:
            raise BizError("SNAPSHOT_INVALID", "开发抽样数量必须为正数", status=422)
        sample_rng = random.Random(draft.get("seed", 20260927))
        data["reflection_item_ids"] = sample_rng.sample(sorted(dev_ids), min(sample_size, len(dev_ids)))
        normalized["data"] = data
        normalized["optimization"] = optimization
        normalized["package_lock"] = {"engine": "prompt-lab", "version": "0.1.0"}
        if strategy == "gepa":
            normalized["package_lock"]["gepa"] = "0.1.4"
        normalized["acceptance"] = (draft.get("acceptance") or {
            "use_sealed_test": True, "n_min_groups": 2})
        return normalized

    def estimate(self, draft: dict, pid: str | None = None) -> dict:
        """费用预估区间：基于样本量与每请求token假设，不含校准与重试（开发方案第8节）。

        07 方案 R08/§16.4：启动前同时估算模型费用与人工评审工时（按“对”计），
        不在启动页固定承诺几十分钟完成。
        """
        dev_n = len((draft.get("data") or {}).get("dev_item_ids") or [])
        cand = int((draft.get("optimization") or {}).get("max_candidates") or 0)
        strategy = (draft.get("optimization") or {}).get("strategy", "reflection")
        if strategy == "gepa":
            metric_cap = int((draft.get("optimization") or {}).get("gepa_max_metric_calls") or 40)
            gen_calls = eval_calls = opt_calls = estimated_tokens = None
            estimate_note = ("GEPA的评价调用上限不是物理请求上限；反思、基线/候选完整复核也会消耗请求。"
                            "不生成误导性的固定token估算，须用运行硬预算限制总消耗。")
        else:
            rounds = 1 if strategy == "single" else cand
            gen_calls = dev_n * (1 + rounds)
            eval_calls = gen_calls
            opt_calls = rounds
            gen_in, gen_out = 1800, 800
            ev_in, ev_out = 2600, 350
            op_in, op_out = 6000, 1600
            estimated_tokens = (gen_calls * (gen_in + gen_out) + eval_calls * (ev_in + ev_out)
                                + opt_calls * (op_in + op_out))
            estimate_note = "为说明计算方法的假设区间，非报价；由硬预算控制上限"
        human = {}
        if pid:
            sealed_n = self.db.one(
                "SELECT COUNT(*) AS c FROM sealed_artifacts WHERE project_id=?"
                " AND access_state='sealed'", (pid,))["c"]
            if sealed_n:
                human = {"review_pairs": sealed_n, "minutes_per_pair": 2,
                         "estimated_minutes": sealed_n * 2,
                         "note": ("按“一对输出约2分钟”的演示口径估算最终盲评工时（未含校准/仲裁）；"
                                  "实际以试标中位耗时为准，不要按“条”误算")}
        result = {"generation_calls": gen_calls, "evaluation_calls": eval_calls,
                "optimizer_calls": opt_calls, "estimated_tokens": estimated_tokens,
                "human_review": human,
                "note": estimate_note}
        if strategy == "gepa":
            result["gepa_metric_call_cap"] = metric_cap
        return result

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
        snapshot = json.loads(json.dumps(snapshot))
        connections = {c["id"]: c for c in _settings("connections", [])}
        for role, config in snapshot["models"].items():
            cid = config.get("connection_id")
            if cid not in connections:
                raise BizError("CONNECTION_NOT_FOUND", f"模型连接不存在：{cid}", status=422)
            connection = connections[cid]
            config["connection_snapshot"] = {key: connection[key] for key in
                ("id", "provider", "base_url", "model", "supports_temperature", "supports_seed", "unsupported_params") if key in connection}
            # Freeze offline fault injection too, so Mock tests exercise the
            # same provider behavior selected when the run was created.
            if connection.get("provider") == "mock" and "mock_inject" in connection:
                config["connection_snapshot"]["mock_inject"] = connection["mock_inject"]
            config["model"] = config.get("model") or connection.get("model") or "default"
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot["data"]["case_bindings"] = freeze_cases(self.db, pid, snapshot["data"])
            sh = canonical_hash(snapshot)
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
        out["acceptance_job"] = AcceptanceService(self.db).job(rid)
        return out

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in
                self.db.query("SELECT id FROM runs WHERE project_id=? ORDER BY created_at DESC",
                              (pid,))]

    # ---------------- 执行（后台线程；协作式取消 TC038；预算暂停）
    def start(self, rid: str) -> None:
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state,project_id,snapshot_json,snapshot_hash FROM runs WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise BizError("NOT_FOUND", "运行不存在", status=404)
            if row["state"] not in ("queued", "paused_budget", "paused_interrupted", "waiting_human"):
                raise BizError("RUN_STATE_INVALID", f"当前状态 {row['state']} 不能启动", status=409)
            if canonical_hash(jloads(row["snapshot_json"], {})) != row["snapshot_hash"]:
                raise BizError("SNAPSHOT_INTEGRITY_INVALID", "实验配置与保存哈希不一致", status=409)
            verify_cases(self.db, row["project_id"], jloads(row["snapshot_json"], {})["data"])
            conn.execute("UPDATE runs SET state='running', stop_reason='', error='' WHERE id=?", (rid,))
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
            verify_cases(self.db, project["id"], snapshot["data"])
            opt = snapshot.get("optimization", {})
            max_rounds = int(opt.get("max_rounds") or opt.get("max_candidates") or 0)
            stall_limit = max(1, int(opt.get("stall_rounds") or 2))
            length_limit = int(opt.get("length_limit_chars") or 4000)
            human_in_loop = bool(opt.get("human_in_loop"))
            target = opt.get("target_score")
            min_delta = float(opt.get("min_delta") or 0.0)
            dev_ids = snapshot["data"]["dev_item_ids"]
            sample = snapshot["data"].get("reflection_item_ids") or dev_ids[: int(opt.get("dev_sample_size") or len(dev_ids))]
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
                                        "search", budget, ledger, execution_key="baseline",
                                        case_bindings=snapshot["data"]["case_bindings"])
                problems = self._check_item_problems(
                    project, baseline_pv, base_res["items"], fb_by_item,
                    snapshot["rubric_id"], models, rid, "search", budget, ledger, execution_key="baseline")
                baseline_detail = {"items": base_res["items"], "score": base_res["score"],
                                   "evaluation_coverage": base_res["evaluation_coverage"],
                                   "usable_rate": base_res["usable_rate"],
                                   "severe": base_res["severe"], "n": base_res["n"],
                                   "problems": problems}
                self.db.execute("UPDATE runs SET baseline_score=?, baseline_detail_json=?,"
                                " current_best_pv=?, best_detail_json=?, updated_at=? WHERE id=?",
                                (base_res["score"],
                                 json.dumps(baseline_detail, ensure_ascii=False),
                                 baseline_pv["id"],
                                 json.dumps({"items": base_res["items"],
                                             "evaluation_coverage": base_res["evaluation_coverage"],
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

            if opt.get("strategy", "reflection") == "gepa":
                stop_reason, current_best_id, candidates, round_i = self._execute_gepa(
                    rid, run, snapshot, project, models, budget, ledger, prompt_svc,
                    current_best_id, candidates, round_i, min_delta)

            while round_i < max_rounds and opt.get("strategy", "reflection") != "gepa":
                verify_cases(self.db, project["id"], snapshot["data"])
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
                failures, correct = self._build_evidence(best_detail, fb_by_item,
                                                       seed=int(snapshot.get("seed", 20260927)) + round_i)
                reflection_sampling = best_detail.get("reflection_sampling", {})
                history = [{"round": rr["round_no"], "decision": rr["decision"],
                            "hypothesis": rr["hypothesis"], "result": rr["status"],
                            "rationale": rr["rationale"]}
                           for rr in self.db.query(
                               "SELECT * FROM run_rounds WHERE run_id=? AND round_no<? ORDER BY round_no", (rid, round_i))]
                pending = self.db.one("SELECT * FROM run_rounds WHERE run_id=? AND round_no=?",
                                      (rid, round_i))
                pending_detail = jloads(pending["detail_json"], {}) if pending else {}
                resumed_candidate = bool(pending and pending["prompt_version_id"] and
                                         pending["status"] in ("candidate_created", "scored"))
                if resumed_candidate:
                    if pending_detail.get("parent_id") != best_pv["id"] or pending_detail.get("parent_hash") != best_pv["hash"]:
                        raise BizError("ROUND_CHECKPOINT_INVALID", "候选检查点与父版本不一致", status=409)
                    rev = pending_detail["revision"]
                else:
                    rev = propose_revision(self.db, models.get("optimizer") or models["generation"],
                                           best_pv, project,
                                           self._rubric_schema(snapshot["rubric_id"]),
                                           {"failures": failures, "correct": correct},
                                           history, rid, budget, ledger, length_limit,
                                           logical_id=f"round:{round_i}:rewrite")
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

                expected_hash = canonical_hash({"body": rev["new_body"], "frozen": best_pv["frozen_segments"],
                                                "variables": best_pv["variables"], "params": best_pv["params"]})
                if resumed_candidate:
                    cand_pv = prompt_svc.get(pending["prompt_version_id"])
                else:
                    existing = self.db.one("SELECT id FROM prompt_versions WHERE project_id=? AND parent_id=? AND hash=? "
                                           "AND origin='optimizer' ORDER BY created_at LIMIT 1",
                                           (run["project_id"], best_pv["id"], expected_hash))
                    cand_pv = prompt_svc.get(existing["id"]) if existing else prompt_svc.create_version(
                        run["project_id"], best_pv["name"], rev["new_body"],
                        best_pv["frozen_segments"], best_pv["variables"], best_pv["params"],
                        parent_id=best_pv["id"], origin="optimizer",
                        hypothesis=rev["hypothesis"])
                actual_hash = canonical_hash({"body": cand_pv["body"], "frozen": cand_pv["frozen_segments"],
                                              "variables": cand_pv["variables"], "params": cand_pv["params"]})
                if actual_hash != expected_hash or cand_pv["hash"] != expected_hash:
                    raise BizError("ROUND_CHECKPOINT_INVALID", "候选内容与保存的改写不一致", status=409)
                checkpoint = {"revision": rev, "parent_id": best_pv["id"], "parent_hash": best_pv["hash"]}
                self._record_round(rid, round_i, status="candidate_created", hypothesis=rev["hypothesis"],
                                   prompt_version_id=cand_pv["id"], detail=checkpoint)
                prompt_svc.validate_candidate(best_pv, cand_pv)  # 冻结段校验：任何收费请求前（TC027）
                cand_res = score_prompt(self.db, project, cand_pv, sample,
                                        snapshot["rubric_id"], models, rid, "search",
                                        budget, ledger, execution_key=f"round:{round_i}:dev",
                                        case_bindings=snapshot["data"]["case_bindings"])
                cand_problems = self._check_item_problems(
                    project, cand_pv, cand_res["items"], fb_by_item,
                    snapshot["rubric_id"], models, rid, "search", budget, ledger,
                    execution_key=f"round:{round_i}:dev")
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
                     "evaluation_coverage": best_detail.get("evaluation_coverage", 1.0),
                     "length": len(best_pv["body"])},
                    {"score": cand_res["score"], "severe": cand_res["severe"],
                     "evaluation_coverage": cand_res["evaluation_coverage"],
                     "length": cand_res["length"], "regressions": regressions,
                     "fixed_problems": len(fixed_ids), "open_problems_before": open_before},
                    min_delta)
                selection_ids = snapshot["data"].get("select_item_ids") or []
                selection_gate = True
                selection_summary = {}
                if selection_ids:
                    selection_base = score_prompt(self.db, project, best_pv, selection_ids,
                                                  snapshot["rubric_id"], models, rid, "search", budget, ledger,
                                                  execution_key=f"round:{round_i}:select:base",
                                                  case_bindings=snapshot["data"]["case_bindings"])
                    selection_candidate = score_prompt(self.db, project, cand_pv, selection_ids,
                                                       snapshot["rubric_id"], models, rid, "search", budget, ledger,
                                                       execution_key=f"round:{round_i}:select:candidate",
                                                       case_bindings=snapshot["data"]["case_bindings"])
                    previous_selection = {it["item_id"]: it for it in selection_base["items"]}
                    selection_regressions = sum(previous_selection[it["item_id"]].get("usable") is True and
                                                it.get("usable") is False for it in selection_candidate["items"])
                    selection_gate = (selection_candidate["evaluation_coverage"] >= selection_base["evaluation_coverage"] and
                                      selection_candidate["score"] > selection_base["score"] + min_delta and
                                      selection_candidate["severe"] <= selection_base["severe"] and selection_regressions == 0)
                    selection_summary = {"item_ids": selection_ids, "baseline_score": selection_base["score"],
                                         "candidate_score": selection_candidate["score"], "passed": selection_gate,
                                         "baseline_coverage": selection_base["evaluation_coverage"],
                                         "candidate_coverage": selection_candidate["evaluation_coverage"],
                                         "baseline_severe": selection_base["severe"],
                                         "candidate_severe": selection_candidate["severe"],
                                         "regressions": selection_regressions, "purpose": "search_selection_not_final_proof"}
                    if not selection_gate:
                        decision = {"decision": "discarded", "rationale": "候选未通过冻结选择案例的提升和非退步检查，保留当前版本"}
                rationale_extra = ""
                if decision["decision"] == "discarded" and regressions == 0 and \
                        selection_gate and \
                        cand_res["evaluation_coverage"] >= best_detail.get("evaluation_coverage", 1.0) and \
                        cand_res["usable_rate"] > (best_detail.get("usable_rate") or 0) and \
                        len(candidates) < 4:
                    decision = dict(decision, decision="retained_alt")
                    rationale_extra = "（虽未成为最优，但可用率更优且无回退：作为各有优势的备选保留，供按偏好选择）"
                cand_entry = {
                    "candidate_id": f"cand_{round_i}", "round": round_i,
                    "prompt_version_id": cand_pv["id"], "hash": cand_pv["hash"],
                    "parent_id": best_pv["id"],
                    "score": cand_res["score"], "usable_rate": cand_res["usable_rate"],
                    "evaluation_coverage": cand_res["evaluation_coverage"],
                    "selection": selection_summary,
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
                                   detail={**checkpoint, "change_summary": rev.get("change_summary", ""),
                                           "reflection_sampling": reflection_sampling,
                                           "selection": selection_summary,
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
                                                        "evaluation_coverage": cand_res["evaluation_coverage"],
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
            if e.code == "CALL_RESULT_UNCONFIRMED":
                self.db.execute("UPDATE runs SET state='paused_interrupted',stop_reason='call_result_unconfirmed',error=?,updated_at=? WHERE id=?",
                                (e.message, now_iso(), rid))
                with self.db.tx() as conn:
                    self._emit(conn, rid, None, "reconciliation_required", {"reason": e.message})
            elif e.code == "BUDGET_EXHAUSTED":
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

    def _execute_gepa(self, rid, run, snapshot, project, models, budget, ledger,
                      prompt_svc, current_best_id, candidates, round_i, min_delta):
        """将可选GEPA策略接入同一账本、冻结案例与候选底线检查。"""
        from .gepa_search import ApplicationSearch
        from .engine import decide_keep

        data = snapshot["data"]
        dev_ids = data["dev_item_ids"]
        select_ids = data["select_item_ids"]
        baseline = prompt_svc.get(snapshot["prompt"]["baseline_id"])
        seed = int(snapshot.get("seed", 20260927))
        metric_limit = int(snapshot["optimization"].get("gepa_max_metric_calls", 40))
        run_dir = Path(self.db.path).parent / ".gepa" / rid
        evaluation_count = 0

        def progress(version, item, result):
            nonlocal evaluation_count
            evaluation_count += 1
            with self.db.tx() as conn:
                self._emit(conn, rid, None, "gepa_evaluation", {
                    "count": evaluation_count,
                    "candidate_id": version["id"] if version else "",
                    "item_id": item["id"], "purpose": item["purpose"],
                    "status": result.get("status", "unknown"),
                    "score": result.get("score")})

        with self.db.tx() as conn:
            self._emit(conn, rid, None, "stage", {
                "stage": "gepa_search", "seed": seed,
                "metric_call_limit": metric_limit, "checkpoint_dir": ".gepa/" + rid})
        self._set_state(rid, "running", "gepa_search")
        search = ApplicationSearch(self.db, project, snapshot, rid, budget, ledger,
                                   lambda: self._cancelled(rid), run_dir=run_dir,
                                   on_evaluation=progress)

        # Baseline and candidate comparisons use identical full dev/select views.
        # The logical ids match ApplicationSearch, so GEPA's own initial evaluations reuse these receipts.
        base_key = "baseline"
        base_dev = score_prompt(self.db, project, baseline, dev_ids, snapshot["rubric_id"],
                                models, rid, "search", budget, ledger,
                                execution_key=base_key, case_bindings=data["case_bindings"])
        base_select = score_prompt(self.db, project, baseline, select_ids, snapshot["rubric_id"],
                                   models, rid, "search", budget, ledger,
                                   execution_key=base_key, case_bindings=data["case_bindings"])
        baseline_detail = {**base_dev, "problems": jloads(self.db.one(
            "SELECT baseline_detail_json FROM runs WHERE id=?", (rid,))["baseline_detail_json"], {}).get("problems", {})}
        self.db.execute("UPDATE runs SET baseline_score=?,baseline_detail_json=?,best_detail_json=?,updated_at=? WHERE id=?",
                        (base_dev["score"], json.dumps(baseline_detail, ensure_ascii=False),
                         json.dumps({"items": base_dev["items"], "evaluation_coverage": base_dev["evaluation_coverage"],
                                     "score": base_dev["score"], "usable_rate": base_dev["usable_rate"],
                                     "severe": base_dev["severe"], "problems": baseline_detail["problems"]},
                                    ensure_ascii=False), now_iso(), rid))

        result = search.run(metric_limit, seed)
        parents = result.get("parents") or []
        frontier = result.get("frontier") or {}
        selection_scores = result.get("selection_scores") or []
        candidate_indices = [i for i, c in enumerate(result["candidates"])
                             if c.get("body") != baseline["body"]]
        by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
        eligible = []
        best_idx = result.get("best_idx")
        cancelled = self._cancelled(rid)

        for idx in candidate_indices:
            item = result["candidates"][idx]
            cand_pv = search.ensure_version(item["body"])
            parent_indices = [p for p in (parents[idx] if idx < len(parents) else [])
                              if isinstance(p, int) and not isinstance(p, bool)]
            parent_ids = [search.ensure_version(result["candidates"][p]["body"])["id"]
                          for p in parent_indices if 0 <= p < len(result["candidates"])]
            parent_id = parent_ids[0] if parent_ids else baseline["id"]
            self.db.execute("UPDATE prompt_versions SET parent_id=? WHERE id=?",
                             (parent_id, cand_pv["id"]))
            cand_pv = prompt_svc.get(cand_pv["id"])
            prompt_svc.validate_candidate(baseline, cand_pv)
            candidate_id = f"gepa_{idx}"
            selection_subscores = (selection_scores[idx] if idx < len(selection_scores) else {})
            frontier_cases = sorted(case_id for case_id, indices in frontier.items()
                                    if idx in indices)

            if cancelled:
                entry = {"candidate_id": candidate_id, "round": idx,
                         "prompt_version_id": cand_pv["id"], "hash": cand_pv["hash"],
                         "parent_id": parent_id, "parent_candidate_ids": parent_ids,
                         "score": None, "usable_rate": None, "evaluation_coverage": None,
                         "selection": {"scores": selection_subscores,
                                       "purpose": "gepa_search_selection_not_final_proof"},
                         "severe": None, "regressions": 0, "fixed_problems": [],
                         "length": len(cand_pv["body"]), "decision": "discarded",
                         "rationale": "运行已停止；候选未完成基线、选择集和底线复核，不能锁定",
                         "evidence": [], "gepa": {"index": idx, "parents": parent_indices,
                             "frontier_cases": frontier_cases, "best_idx": best_idx,
                             "seed": seed, "sdk_version": "0.1.4"}}
                by_id[candidate_id] = entry
                continue

            execution_key = f"gepa:{canonical_hash({'body': item['body']})}"
            cand_dev = score_prompt(self.db, project, cand_pv, dev_ids, snapshot["rubric_id"],
                                    models, rid, "search", budget, ledger,
                                    execution_key=execution_key, case_bindings=data["case_bindings"])
            cand_select = score_prompt(self.db, project, cand_pv, select_ids, snapshot["rubric_id"],
                                       models, rid, "search", budget, ledger,
                                       execution_key=execution_key, case_bindings=data["case_bindings"])
            prev_usable = {it["item_id"]: it.get("usable") for it in base_dev["items"]}
            regressions = sum(1 for it in cand_dev["items"]
                              if prev_usable.get(it["item_id"]) is True and it.get("usable") is False)
            base_sel_usable = {it["item_id"]: it.get("usable") for it in base_select["items"]}
            select_regressions = sum(1 for it in cand_select["items"]
                                     if base_sel_usable.get(it["item_id"]) is True and
                                     it.get("usable") is False)
            selection_gate = (
                cand_select["evaluation_coverage"] >= base_select["evaluation_coverage"] and
                cand_select["score"] > base_select["score"] + min_delta and
                cand_select["severe"] <= base_select["severe"] and select_regressions == 0)
            decision = decide_keep(
                {"score": base_dev["score"], "severe": base_dev["severe"],
                 "evaluation_coverage": base_dev["evaluation_coverage"],
                 "length": len(baseline["body"])},
                {"score": cand_dev["score"], "severe": cand_dev["severe"],
                 "evaluation_coverage": cand_dev["evaluation_coverage"],
                 "length": len(cand_pv["body"]), "regressions": regressions}, min_delta)
            rationale = decision["rationale"]
            decision_name = decision["decision"]
            if not selection_gate:
                decision_name = "discarded"
                rationale += "；固定选择案例未通过预先冻结的提升、覆盖和底线检查"
            selection_summary = {"item_ids": select_ids,
                                 "baseline_score": base_select["score"],
                                 "candidate_score": cand_select["score"],
                                 "passed": selection_gate,
                                 "baseline_coverage": base_select["evaluation_coverage"],
                                 "candidate_coverage": cand_select["evaluation_coverage"],
                                 "baseline_severe": base_select["severe"],
                                 "candidate_severe": cand_select["severe"],
                                 "regressions": select_regressions,
                                 "purpose": "search_selection_not_final_proof"}
            entry = {"candidate_id": candidate_id, "round": idx,
                     "prompt_version_id": cand_pv["id"], "hash": cand_pv["hash"],
                     "parent_id": parent_id, "parent_candidate_ids": parent_ids,
                     "score": cand_dev["score"], "usable_rate": cand_dev["usable_rate"],
                     "evaluation_coverage": cand_dev["evaluation_coverage"],
                     "selection": selection_summary, "severe": cand_dev["severe"],
                     "regressions": regressions, "fixed_problems": [],
                     "length": cand_dev["length"], "decision": decision_name,
                     "rationale": rationale, "evidence": cand_dev["failures"][:5],
                     "gepa": {"index": idx, "parents": parent_indices,
                              "selection_scores": selection_subscores,
                              "frontier_cases": frontier_cases, "best_idx": best_idx,
                              "seed": seed, "sdk_version": "0.1.4"}}
            by_id[candidate_id] = entry
            round_i = max(round_i, idx)
            self._record_round(rid, idx, status="scored", hypothesis="GEPA候选",
                               prompt_version_id=cand_pv["id"], score=cand_dev["score"],
                               prev_score=base_dev["score"], usable_rate=cand_dev["usable_rate"],
                               severe=cand_dev["severe"], regressions=regressions,
                               decision=decision_name, rationale=rationale,
                               length_chars=cand_dev["length"],
                               detail={"strategy": "gepa", "parents": parent_indices,
                                       "selection": selection_summary,
                                       "frontier_cases": frontier_cases, "seed": seed,
                                       "sdk_version": "0.1.4"})
            candidates = list(by_id.values())
            self._persist_progress(rid, round_i, current_best_id, 0,
                                   candidates=candidates)
            if decision_name in ("kept", "retained_alt"):
                eligible.append((cand_select["score"], candidate_id, cand_pv["id"], cand_dev))

        candidates = list(by_id.values())
        if eligible:
            preferred = next((row for row in eligible if row[1] == f"gepa_{best_idx}"), None)
            selected = preferred or max(eligible, key=lambda row: row[0])
            _, candidate_id, current_best_id, best_dev = selected
            self._persist_progress(rid, round_i, current_best_id, 0,
                                   best_detail={"items": best_dev["items"],
                                                "evaluation_coverage": best_dev["evaluation_coverage"],
                                                "score": best_dev["score"],
                                                "usable_rate": best_dev["usable_rate"],
                                                "severe": best_dev["severe"], "problems": {}},
                                   candidates=candidates)
            stop_reason = "gepa_candidate_found"
        elif cancelled:
            stop_reason = "user_cancelled"
            self._persist_progress(rid, round_i, current_best_id, 0, candidates=candidates)
        else:
            stop_reason = "no_improvement"
            self._persist_progress(rid, round_i, current_best_id, 0, candidates=candidates)

        with self.db.tx() as conn:
            self._emit(conn, rid, None, "gepa_completed", {
                "strategy": "gepa", "sdk_version": "0.1.4", "seed": seed,
                "metric_calls": result.get("metric_calls"), "best_idx": best_idx,
                "candidate_count": len(candidate_indices), "stop_reason": stop_reason,
                "checkpoint_dir": ".gepa/" + rid})
        return stop_reason, current_best_id, candidates, round_i

    # ---- 编排辅助 ----
    def _rubric_schema(self, rubric_id: str) -> dict:
        r = self.db.one("SELECT schema_json FROM rubrics WHERE id=?", (rubric_id,))
        return json.loads(r["schema_json"]) if r else {}

    def _build_evidence(self, best_detail: dict, fb_by_item: dict, seed: int = 20260927) -> tuple[list[dict], list[dict]]:
        """组装优化器证据（§8.2）：失败案例带完整输入/输出/专家意见；正确案例要求保持。"""
        items = best_detail.get("items", [])
        scored = [it for it in items if it.get("score") is not None]
        from prompt_core.reflection import sample_reflection
        failures, sampling = sample_reflection(items, 5, seed)
        best_detail["reflection_sampling"] = sampling
        correct = [it for it in scored if it.get("usable") and not it.get("severe")][:3]
        def pack(it):
            fb_rows = fb_by_item.get(it["item_id"], [])
            return {"case_id": it["case_id"], "item_id": it["item_id"],
                    "runtime_input": it.get("runtime_input", {}),
                    "output_text": it.get("output_text", ""), "dims": it.get("dims", {}),
                    "generation_status": it.get("gen_status"), "evaluation_unknown": bool(it.get("eval_abstain")),
                    "feedback": [{"problem": f["problem"], "quote": f["quote"],
                                  "expected": f["expected"], "severity": f["severity"],
                                  "status": f["status"], "tags": f["tags"],
                                  "remark": f["remark"]} for f in fb_rows]}
        return [pack(it) for it in failures], [pack(it) for it in correct]

    def _check_item_problems(self, project, prompt_pv, items_detail, fb_by_item,
                             rubric_id, models, rid, phase, budget, ledger, *, execution_key=None) -> dict:
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
                                 phase, budget, ledger,
                                 logical_id=f"{execution_key}:problems:{prompt_pv['id']}:{it['item_id']}" if execution_key else None)
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
        if run["state"] not in ("paused_budget", "paused_interrupted"):
            raise BizError("RUN_STATE_INVALID", "只有预算暂停或启动后待恢复的运行可以恢复", status=409)
        self.start(rid)
        return self.get(rid)

    def recover_interrupted(self) -> list[str]:
        """Startup-only recovery; caller must hold the data directory OS lock."""
        recovered = []
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE ledger SET status='sent_unknown' WHERE status='reserved'")
            conn.execute("UPDATE severity_audits SET state='paused_interrupted',error='服务中断，可继续原审计',revision=revision+1,updated_at=? WHERE state='running'",(now_iso(),))
            conn.execute("UPDATE acceptance_jobs SET state='cancelled',error='考试已停止，考题暴露与已完成结果保留',updated_at=? WHERE state='cancelling'",
                         (now_iso(),))
            conn.execute("UPDATE acceptance_jobs SET state='paused_interrupted',error='服务中断，可继续同一考试',updated_at=? WHERE state='running'",
                         (now_iso(),))
            rows = conn.execute("SELECT * FROM runs WHERE state IN ('queued','running','stopping')").fetchall()
            for row in rows:
                state = "cancelled" if row["state"] == "stopping" else "paused_interrupted"
                reason = "user_cancelled" if state == "cancelled" else "service_interrupted"
                try:
                    budget = BudgetState(jloads(row["budget_state_json"], {}))
                    Ledger._refresh(conn, row["id"], budget)
                except BizError as exc:
                    conn.execute("UPDATE runs SET state='failed',error=?,updated_at=? WHERE id=?",
                                 (exc.message, now_iso(), row["id"]))
                    continue
                conn.execute("UPDATE runs SET state=?,stop_reason=?,budget_state_json=?,revision=revision+1,updated_at=? WHERE id=?",
                             (state, reason, json.dumps(budget.to_json()), now_iso(), row["id"]))
                self._emit(conn, row["id"], None, "startup_recovered", {
                    "previous_state": row["state"], "state": state,
                    "note": "未确认调用保留预留，已保存结果复用；不会自动重发旧请求"})
                recovered.append(row["id"])
        return recovered

    def update_budget(self, rid: str, limits: dict, revision: int) -> dict:
        fields = ("total_limit", "search_limit", "acceptance_limit")
        if (not isinstance(limits, dict) or set(limits) != set(fields)
                or isinstance(revision, bool) or not isinstance(revision, int)):
            raise BizError("BUDGET_UPDATE_INVALID", "须提供三项新额度及当前revision", status=422)
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise BizError("NOT_FOUND", "运行不存在", status=404)
            exam = conn.execute("SELECT state FROM acceptance_jobs WHERE run_id=?", (rid,)).fetchone()
            exam_paused = row["state"] == "completed" and exam and exam["state"] == "paused_budget"
            if row["state"] != "paused_budget" and not exam_paused:
                raise BizError("RUN_STATE_INVALID", "只有预算暂停时可以增加额度", status=409)
            if row["revision"] != revision:
                raise BizError("REVISION_CONFLICT", "运行已变化，请刷新后重试", status=409)
            current = BudgetState(jloads(row["budget_state_json"], {}))
            Ledger._refresh(conn, rid, current)
            updated = BudgetState({**current.to_json(), **limits})
            updated.validate()
            if any(getattr(updated, field) < getattr(current, field) for field in fields):
                raise BizError("BUDGET_CANNOT_DECREASE", "增加额度不能降低总额、搜索或验收预留", status=422)
            if all(getattr(updated, field) == getattr(current, field) for field in fields):
                raise BizError("BUDGET_NOT_INCREASED", "请明确增加额度后再恢复", status=422)
            conn.execute("UPDATE runs SET budget_state_json=?,revision=revision+1,updated_at=? WHERE id=?",
                         (json.dumps(updated.to_json(), ensure_ascii=False), now_iso(), rid))
            self._emit(conn, rid, None, "budget_increased", {
                "before": {field: current.to_json()[field] for field in fields},
                "after": {field: updated.to_json()[field] for field in fields},
                "mode": current.mode, "revision": revision + 1})
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
        # The service OS lock protects against multiple processes. Serialize
        # concurrent requests in this process before checking/writing reports.
        with _acceptance_locks.setdefault((str(self.db.path), rid), threading.Lock()):
            try:
                return self._accept(rid)
            except Exception as exc:
                current_job = self.job(rid)
                if current_job and current_job["state"] == "cancelling":
                    self._check_cancelled(current_job["id"])
                state = "paused_budget" if isinstance(exc, BizError) and exc.code == "BUDGET_EXHAUSTED" else "paused_interrupted"
                message = exc.message if isinstance(exc, BizError) else "考试中断，已完成结果保留"
                self.db.execute("UPDATE acceptance_jobs SET state=?,error=?,updated_at=? WHERE run_id=? AND state='running'",
                                (state, message, now_iso(), rid))
                raise

    def job(self, rid: str) -> dict | None:
        row = self.db.one("SELECT id,state,error,report_id,updated_at,manifest_json FROM acceptance_jobs WHERE run_id=?", (rid,))
        if row is None:
            return None
        return {key: row[key] for key in ("id", "state", "error", "report_id", "updated_at")} | {
            "case_count": len(jloads(row["manifest_json"], []))}

    def cancel(self, rid: str) -> dict:
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id,state FROM acceptance_jobs WHERE run_id=?", (rid,)).fetchone()
            if row is None:
                raise BizError("NOT_FOUND", "考试任务不存在", status=404)
            if row["state"] == "completed":
                raise BizError("EXAM_STATE_INVALID", "考试已完成，不能取消报告", status=409)
            state = "cancelling" if row["state"] in ("running", "cancelling") else "cancelled"
            conn.execute("UPDATE acceptance_jobs SET state=?,error='已请求停止，保留考题暴露与已完成结果',updated_at=? WHERE id=?",
                         (state, now_iso(), row["id"]))
            RunService(self.db)._emit(conn, rid, None, "acceptance_cancel_requested", {"job_id": row["id"], "state": state})
        return self.job(rid)

    def _check_cancelled(self, job_id: str) -> None:
        row = self.db.one("SELECT state FROM acceptance_jobs WHERE id=?", (job_id,))
        if row and row["state"] in ("cancelling", "cancelled"):
            self.db.execute("UPDATE acceptance_jobs SET state='cancelled',updated_at=? WHERE id=?", (now_iso(), job_id))
            raise BizError("EXAM_CANCELLED", "考试已停止，已完成结果和考题暴露记录保留", status=409)

    def _accept(self, rid: str) -> dict:
        """Bind once before dispatch; resume the same manifest and logical calls."""
        run = RunService(self.db).get(rid)
        if run["state"] != "completed":
            raise BizError("RUN_STATE_INVALID", "运行尚未完成", status=409)
        if not run["locked_candidate"]:
            raise BizError("CANDIDATE_NOT_LOCKED", "必须先锁定最终候选才能申请独立验收（TC042）",
                           status=409)
        snapshot = run["snapshot"]
        if canonical_hash(snapshot) != run["snapshot_hash"]:
            raise BizError("SNAPSHOT_INTEGRITY_INVALID", "实验配置与保存哈希不一致", status=409)
        # 评价器有效性：stale评价器不能做自动确认结论（BR10/TC024）
        human_mode = snapshot.get("acceptance", {}).get("evaluation_source") == "human"
        judge_id = snapshot.get("judge_id")
        judge_admitted = False
        if judge_id:
            jr = self.db.one("SELECT status,metrics_json,rubric_id,model_config_json FROM judges WHERE id=?", (judge_id,))
            if jr and jr["status"] == "stale" and not human_mode:
                raise BizError("JUDGE_STALE", "评价器已stale，不能用于确认性验收", status=422)
            from .judge_binding import admitted_for
            judge_admitted = admitted_for(self.db, jr, snapshot["rubric_id"], snapshot["models"]["evaluation"])
        # 先解析候选身份，再检查考题绑定（07 方案 R03/16.5：一次绑定原则）
        candidates = {c["candidate_id"]: c for c in run["candidates"]}
        if run["locked_candidate"] == "baseline":
            cand_pv_id = run["baseline_prompt_id"]
        else:
            cand_pv_id = candidates[run["locked_candidate"]]["prompt_version_id"]
        baseline_pv_id = run["baseline_prompt_id"]
        prev = self.db.one(
            "SELECT * FROM acceptance_reports WHERE run_id=? AND candidate_ref=?"
            " ORDER BY created_at DESC LIMIT 1", (rid, cand_pv_id))
        if prev is not None:
            self.db.execute("UPDATE acceptance_jobs SET report_id=?,state='completed',error='',updated_at=? WHERE run_id=? AND candidate_ref=?",
                            (prev["id"], now_iso(), rid, cand_pv_id))
            # 同一运行 + 同一候选 + 同一协议：幂等续跑，返回原报告（TC064）
            out = self.get(prev["id"])
            out["idempotent"] = True
            out["idempotent_note"] = "该候选的独立验收已存在，返回原报告（未重复消耗考题，TC064）"
            return out
        bound = self.db.one(
            "SELECT * FROM acceptance_reports WHERE project_id=? AND consumed=1"
            " ORDER BY created_at DESC LIMIT 1", (run["project_id"],))
        if bound is not None:
            raise BizError(
                "TEST_ALREADY_CONSUMED",
                f"该批考题已绑定验收报告 {bound['id']}（候选 {bound['candidate_ref']}，"
                f"{bound['created_at']}）。按一次绑定原则，即使只看过汇总分数，"
                "同一考题也不能为新候选提供新的独立证明；请补充新的独立考题（TC063）。",
                field_errors={"bound_report": bound["id"],
                              "bound_candidate": bound["candidate_ref"]}, status=409)
        from .acceptance_jobs import bind_exam
        from .domain import PromptService
        project = ProjectsService(self.db).get(run["project_id"])
        pv_base = PromptService(self.db).get(baseline_pv_id)
        pv_cand = PromptService(self.db).get(cand_pv_id)
        job = bind_exam(self.db, run, pv_base, pv_cand, project["contract"])
        self._check_cancelled(job["id"])
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT state FROM acceptance_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            if state in ("cancelled", "cancelling"):
                raise BizError("EXAM_CANCELLED", "考试已停止，不能重新启动", status=409)
            conn.execute("UPDATE acceptance_jobs SET state='running',error='',updated_at=? WHERE id=?", (now_iso(), job["id"]))
        project["contract"] = jloads(job["contract_json"], {})
        sealed = jloads(job["manifest_json"], [])
        artifact_total = job["artifact_total"]
        models = snapshot["models"]
        budget = BudgetState(run["budget"])
        ledger = Ledger(self.db)
        rubric_id = snapshot["rubric_id"]

        pairs = []
        human_pending = False
        from .human_acceptance import HumanAcceptance
        human_review = HumanAcceptance(self.db)
        severe_base = severe_cand = unknown = 0
        fb_by_item: dict[str, list[dict]] = {}
        for f in FeedbackService(self.db).list(run["project_id"]):
            if f["item_id"] and f["status"] not in ("resolved", "retired"):
                fb_by_item.setdefault(f["item_id"], []).append(f)
        from .engine import check_problems, evaluate_once
        from .providers import has_severe_error, is_usable
        for art in sealed:
            self._check_cancelled(job["id"])
            item = {"id": art["item_id"], "case_id": art["item_id"],
                    "runtime_input": art["runtime_input"], "source_group_id": art.get("source_group_id")}
            # 交错执行：基线与候选同输入/同参数/同模型（TC042）
            ob = generate_once(self.db, project, pv_base, item, models["generation"], rid,
                               "acceptance", budget, ledger,
                               logical_id=f"{job['id']}:base:generation:{art['item_id']}", pause_on_unconfirmed=True)
            self._check_cancelled(job["id"])
            oc = generate_once(self.db, project, pv_cand, item, models["generation"], rid,
                               "acceptance", budget, ledger,
                               logical_id=f"{job['id']}:candidate:generation:{art['item_id']}", pause_on_unconfirmed=True)
            row_b = self.db.one("SELECT text FROM outputs WHERE id=?", (ob["id"],))
            row_c = self.db.one("SELECT text FROM outputs WHERE id=?", (oc["id"],))
            self._check_cancelled(job["id"])
            if human_mode:
                registered = human_review.register(job["id"], art["item_id"], ob, oc)
                reviewed = human_review.decisions(registered)
                if reviewed is None:
                    human_pending = True
                    continue
                pairs.append({"item_id": art["item_id"], "source_group_id": art.get("source_group_id"),
                              "base_ok": ob["status"] == "ok", "cand_ok": oc["status"] == "ok",
                              "base_usable": reviewed['baseline']['usable'],
                              "cand_usable": reviewed['candidate']['usable'],
                              "base_severe": reviewed['baseline']['severe'],
                              "cand_severe": reviewed['candidate']['severe']})
                severe_base += reviewed['baseline']['severe'] is True
                severe_cand += reviewed['candidate']['severe'] is True
                unknown += reviewed['baseline']['usable'] is None or reviewed['candidate']['usable'] is None
                continue
            sb = evaluate_once(row_b["text"], rubric_id, models["evaluation"], rid, "acceptance",
                               budget=budget, ledger=ledger, task_input=item["runtime_input"],
                               evaluation_reference=art.get("evaluation_only", {}),
                               logical_id=f"{job['id']}:base:evaluation:{art['item_id']}", pause_on_unconfirmed=True) \
                if ob["status"] == "ok" else {"abstain": True}
            self._check_cancelled(job["id"])
            sc = evaluate_once(row_c["text"], rubric_id, models["evaluation"], rid, "acceptance",
                               budget=budget, ledger=ledger, task_input=item["runtime_input"],
                               evaluation_reference=art.get("evaluation_only", {}),
                               logical_id=f"{job['id']}:candidate:evaluation:{art['item_id']}", pause_on_unconfirmed=True) \
                if oc["status"] == "ok" else {"abstain": True}
            entry = {"item_id": art["item_id"], "base_ok": ob["status"] == "ok",
                     "source_group_id": art.get("source_group_id"),
                     "cand_ok": oc["status"] == "ok",
                     "base_usable": False if ob["status"] != "ok" else is_usable(sb) if not sb.get("abstain") else None,
                     "cand_usable": False if oc["status"] != "ok" else is_usable(sc) if not sc.get("abstain") else None,
                     "base_severe": has_severe_error(row_b["text"], sb) if not sb.get("abstain")
                     and ob["status"] == "ok" else False if ob["status"] != "ok" else None,
                     "cand_severe": has_severe_error(row_c["text"], sc) if not sc.get("abstain")
                     and oc["status"] == "ok" else False if oc["status"] != "ok" else None}
            # 问题级核查（§9.3 问题项口径）：仅对登记了专家意见的封存案例发起调用
            fbs = fb_by_item.get(art["item_id"]) or []
            if fbs and ob["status"] == "ok" and oc["status"] == "ok":
                plist = [{"id": f["id"], "problem": f["problem"], "expected": f["expected"]}
                         for f in fbs]
                self._check_cancelled(job["id"])
                entry["base_problem_status"] = check_problems(
                    self.db, plist, row_b["text"], models["evaluation"], rid,
                    "acceptance", budget, ledger,
                    logical_id=f"{job['id']}:base:problems:{art['item_id']}")["statuses"]
                self._check_cancelled(job["id"])
                entry["cand_problem_status"] = check_problems(
                    self.db, plist, row_c["text"], models["evaluation"], rid,
                    "acceptance", budget, ledger,
                    logical_id=f"{job['id']}:candidate:problems:{art['item_id']}")["statuses"]
            if entry["base_severe"]:
                severe_base += 1
            if entry["cand_severe"]:
                severe_cand += 1
            if entry["base_usable"] is None or entry["cand_usable"] is None:
                unknown += 1
            pairs.append(entry)

        self._check_cancelled(job["id"])
        if human_pending:
            with self.db.tx() as conn:
                conn.execute('BEGIN IMMEDIATE')
                state = conn.execute('SELECT state FROM acceptance_jobs WHERE id=?', (job['id'],)).fetchone()[0]
                if state != 'running':
                    raise BizError('EXAM_CANCELLED', '考试已请求停止，不开放人工评审', status=409)
                conn.execute("UPDATE acceptance_jobs SET state='waiting_human',updated_at=? WHERE id=?", (now_iso(), job['id']))
            return {"state": "waiting_human", "job": self.job(rid)}
        if human_mode:
            human_evidence = human_review.evidence(rid)
            judge_admitted = True  # Every side has an irreversible named human decision.
        known = [p for p in pairs if p["base_usable"] is not None and p["cand_usable"] is not None]
        g = [(p["source_group_id"], p["base_usable"], p["cand_usable"]) for p in known]
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
        measurement = summarize_pairs([{"baseline": p["base_usable"], "candidate": p["cand_usable"]}
                                       for p in pairs])
        mb = measurement["bounds"]["candidate"]
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
        elif (measurement["worst_case"]["gain"] >= min_impr and
              measurement["worst_case"]["p"] < 0.05 and all(p["cand_severe"] is False for p in pairs)):
            decision = "verified_improvement"
        elif boot["ci_high"] < 0:
            decision = "regression"
        else:
            decision = "no_improvement"

        # ---- 07 方案 R12/§17：四层分开——证据 / 质量 / 门槛 / 采用资格 ----
        min_groups = int(policy.get("min_test_groups", 5))
        if judge_admitted and not human_mode:
            current_judge = self.db.one("SELECT * FROM judges WHERE id=?", (judge_id,))
            if not admitted_for(self.db, current_judge, snapshot["rubric_id"], snapshot["models"]["evaluation"]):
                raise BizError("CALIBRATION_CONTEXT_CHANGED", "验收期间校准证据或评价配置发生变化，请核查后恢复", status=409)
        unknown_ratio = unknown / max(1, len(pairs))
        # Ordinal agreement does not validate the separate severe-error detector.
        from .judge_binding import severe_admitted_for
        severe_admitted = human_mode or bool(judge_admitted and severe_admitted_for(
            self.db,current_judge,snapshot['rubric_id'],snapshot['models']['evaluation']))
        risk_known = all(p["cand_severe"] is not None for p in pairs)
        sev_upper = zero_event_upper_bound(len(pairs)) if severe_cand == 0 and risk_known else None
        gate_severe = severe_admitted and severe_cand == 0 and all(p["cand_severe"] is False for p in pairs)
        gate_sample = n >= min_groups
        gate_evidence = n > 0 and unknown_ratio <= 0.2 and judge_admitted
        gates = {
            "评价器准入": {"result": "通过" if judge_admitted else "需人工复核",
                      "detail": "冻结标准下全部输出逐侧由具名人工提交并锁定。" if human_mode else "评分校准须匹配标准内容、模型、参数、连接及评价程序；旧校准不能跨配置复用。"},
            "严重错误门槛": {"result": "需人工复核" if not severe_admitted or not risk_known else "通过" if gate_severe else "未通过",
                        "detail": ("严重错误判断尚未经独立人工金标校准，不能由普通分数一致性推断安全。" if not severe_admitted else
                                   "候选存在严重风险无法判断项，需要独立核验，不能按零风险处理。" if not risk_known else
                                   f"候选严重错误 {severe_cand} 个（基线 {severe_base} 个）"
                                   if not gate_severe else
                                   f"候选严重错误 0 观察；95%置信上界 "
                                   f"{(sev_upper * 100):.2f}%" if sev_upper is not None
                                   else "候选严重错误 0 观察")},
            "样本充足门槛": {"result": "通过" if gate_sample else "不足",
                        "detail": f"独立考题 {n} 组（建议至少 {min_groups} 组）"},
            "证据有效性": {"result": "有效" if gate_evidence else "资料不足",
                      "detail": f"无法判断 {unknown}/{len(pairs)} 条，未从分母静默删除"},
        }
        if not judge_admitted:
            decision = "evaluation_invalid"
        quality_map = {
            "verified_improvement": "改善",
            "no_improvement": "尚未证明改善",
            "regression": "退步",
            "inconclusive": "无法判断（证据不足）",
            "evaluation_invalid": "无法判断（评价无效）",
        }
        quality_decision = quality_map.get(decision, decision)
        evidence_status = "有效" if gate_evidence else "资料不足"
        # 决策顺序（§17）：先证据有效性，再严重错误阻断，再看质量与样本
        if not gate_evidence:
            eligibility = ("暂缓正式采用（先完成评价器校准或人工复核）" if not judge_admitted
                           else "暂缓正式采用（补充考题后重新验证）")
            reason_codes = ["证据不足：评价器未准入或无法判断项占比过高，不产生正式改善结论"]
        elif not severe_admitted:
            eligibility = "暂缓正式采用（严重风险尚待独立人工核验）"
            reason_codes = ["严重错误判断未准入：评分一致性不能证明风险判断可靠"]
        elif not risk_known:
            eligibility = "暂缓正式采用（严重风险存在无法判断项）"
            reason_codes = ["严重风险无法判断，不能当作没有严重错误"]
        elif not gate_severe:
            eligibility = "不可正式采用（保留原版）"
            reason_codes = ["严重错误门槛未通过：候选出现严重错误，均分不能掩盖（TC046）"]
        elif decision == "verified_improvement":
            if gate_sample:
                eligibility = "可正式采用（仍需负责人确认）"
                reason_codes = ["主指标改善且配对区间下界>0", "严重错误门槛通过", "样本充足"]
            else:
                eligibility = "暂缓正式采用（补充考题后重新验证）"
                reason_codes = [f"样本不足：考题仅 {n} 组（建议≥{min_groups}）",
                                "趋势正向有希望，但尚未证明改善；可先保存为试用版本"]
        elif decision == "no_improvement":
            eligibility = "保留原版"
            reason_codes = ["未见改善：未证实改善不等于已证明等效；保留原版为正常结果"]
        elif decision == "regression":
            eligibility = "不可正式采用（保留原版）"
            reason_codes = ["质量退步或严重错误阻断"]
        else:
            eligibility = "暂缓正式采用"
            reason_codes = ["证据不足，无法判断"]
        acceptance_id = job["id"]
        from .domain import PromptService as _PS
        cand_hash = _PS(self.db).get(cand_pv_id)["hash"]
        base_hash = _PS(self.db).get(baseline_pv_id)["hash"]
        policy_hash = canonical_hash(policy)

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
            "sealed_artifact_total": artifact_total, "measurement": measurement,
            "acceptance_usage": ledger.phase_summary(rid, "acceptance"),
            "sampling_unit": "one_case_per_source_group",
            "baseline_usable_rate": base_rate, "candidate_usable_rate": cand_rate,
            "diff": diff, "fix": cand_only, "regress": base_only, "both": both, "neither": neither,
            "mcnemar_p": p_mcnemar, "bootstrap": boot,
            "missing_bounds": mb, "severe_baseline": severe_base, "severe_candidate": severe_cand,
            "severe_unknown_candidate": sum(p['cand_severe'] is None for p in pairs),
            "severe_upper_bound_95": sev_upper if severe_admitted else None,
            "severe_note": ("严重错误检测未准入；自动观察计数不能作为真实风险置信上界。" if not severe_admitted else
                            "候选存在严重风险unknown，不能计算零事件风险上界。" if not risk_known else
                            "候选严重错误为0观察：单侧95%上界约{:.2%}，不等于真实零风险（TC047）"
                            .format(sev_upper) if sev_upper is not None else ""),
            "evaluator_evidence": {"judge_id": judge_id, "score_admitted": judge_admitted,
                                   "source": "human" if human_mode else "model",
                                   "human_evidence": human_evidence if human_mode else None,
                                   "human_context_hash": human_review.context_hash(job) if human_mode else None,
                                   "severe_admitted": severe_admitted,
                                   "binding": jloads(jr["metrics_json"], {}).get("evaluator_binding") if judge_id and jr else None},
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
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT state FROM acceptance_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            if state != "running":
                raise BizError("EXAM_CANCELLED", "考试已请求停止，不生成报告", status=409)
            conn.execute(
                "INSERT INTO acceptance_reports(id,run_id,project_id,candidate_ref,baseline_ref,"
                "test_manifest_json,policy_json,stats_json,decision,consumed,created_at,"
                "acceptance_id,candidate_hash,baseline_hash,policy_hash,"
                "evidence_status,quality_decision,gates_json,eligibility,reason_codes_json)"
                " VALUES(?,?,?,?,?,?,?,?,?, '1', ?,?,?,?,?,?,?,?,?,?)",
                (rep_id, rid, run["project_id"], cand_pv_id, baseline_pv_id,
                 json.dumps({"sealed_items": [p["item_id"] for p in pairs],
                             "acceptance_id": acceptance_id,
                             "candidate_hash": cand_hash, "baseline_hash": base_hash,
                             "policy_hash": policy_hash}, ensure_ascii=False),
                 json.dumps(policy, ensure_ascii=False), json.dumps(stats, ensure_ascii=False),
                 decision, now_iso(), acceptance_id, cand_hash, base_hash, policy_hash,
                 evidence_status, quality_decision,
                 json.dumps(gates, ensure_ascii=False), eligibility,
                 json.dumps(reason_codes, ensure_ascii=False)))
            conn.execute("UPDATE acceptance_jobs SET report_id=?,state='completed',error='',updated_at=? WHERE id=?",
                         (rep_id, now_iso(), job["id"]))
        # 考题曝光事件（R03/16.5：暴露与用途消耗可审计，不依赖用户自报）
        RunService(self.db)._audit("sealed.exposure",
                                   f"{rep_id}|candidate={cand_hash[:12]}|protocol={policy_hash[:12]}",
                                   canonical_hash({"sealed": [p["item_id"] for p in pairs]}))
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
                "stats": jloads(r["stats_json"], {}), "created_at": r["created_at"],
                # 07 方案四层分开：证据状态 / 质量结论 / 门槛 / 采用资格（旧报告字段为空）
                "acceptance_id": r["acceptance_id"] or "",
                "binding": {"candidate_hash": (r["candidate_hash"] or "")[:16],
                            "baseline_hash": (r["baseline_hash"] or "")[:16],
                            "policy_hash": (r["policy_hash"] or "")[:16]},
                "evidence_status": r["evidence_status"] or "",
                "quality_decision": r["quality_decision"] or "",
                "gates": jloads(r["gates_json"], {}),
                "eligibility": r["eligibility"] or "",
                "reason_codes": jloads(r["reason_codes_json"], [])}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query(
            "SELECT id FROM acceptance_reports WHERE project_id=? ORDER BY created_at DESC", (pid,))]


# ---------------------------------------------------------------- 使用版本与反馈 P13

class ReleaseService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def adopt(self, pid: str, prompt_version_id: str, report_ref: str, mode: str = "active",
              expected_revision: int | None = None, model_config: dict | None = None) -> dict:
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._adopt(pid,prompt_version_id,report_ref,mode,expected_revision,model_config)

    def _adopt(self, pid: str, prompt_version_id: str, report_ref: str, mode: str = "active",
             expected_revision: int | None = None, model_config: dict | None = None) -> dict:
        ProjectsService(self.db).ensure_active(pid)
        if mode not in ("active", "trial"):
            raise BizError("RELEASE_MODE_INVALID", "采用方式必须是 active 或 trial", status=422)
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
            evaluator_evidence = jloads(rep["stats_json"], {}).get("evaluator_evidence", {})
            if evaluator_evidence.get('source') not in ('human','model'):
                raise BizError('REPORT_EVALUATOR_MISSING','旧报告缺少评价来源及证据，须重新验收后正式采用',status=422)
            if evaluator_evidence.get("source") == "human":
                from .human_acceptance import HumanAcceptance
                actual = HumanAcceptance(self.db).evidence(rep["run_id"])
                job = self.db.one('SELECT state,report_id FROM acceptance_jobs WHERE run_id=?', (rep['run_id'],))
                if actual != evaluator_evidence.get('human_evidence') or not job or job['state'] != 'completed' or job['report_id'] != report_ref:
                    raise BizError('HUMAN_EVIDENCE_CHANGED', '人工验收证据与报告不一致，不能发布', status=409)
            from .domain import PromptService
            pv_hash = PromptService(self.db).get(prompt_version_id)["hash"]
            gates = jloads(rep["gates_json"], {})
            decision = adoption_decision(True, {
                "binding": bool(rep["candidate_hash"]) and rep["candidate_hash"] == pv_hash,
                "evidence": rep["evidence_status"] == "有效" and
                    gates.get("证据有效性", {}).get("result") == "有效" and
                    gates.get("评价器准入", {}).get("result") == "通过",
                "sample": gates.get("样本充足门槛", {}).get("result") == "通过",
                "safety": gates.get("严重错误门槛", {}).get("result") == "通过",
                "protocol": bool(rep["policy_hash"]) and
                    rep["policy_hash"] == canonical_hash(jloads(rep["policy_json"], {})),
            })
            if not decision["eligible"]:
                raise BizError("REPORT_NOT_ELIGIBLE", "验收门槛未通过或报告绑定不完整，只能试用",
                               field_errors={"reasons": decision["reasons"]}, status=422)
            experiment = self.db.one("SELECT snapshot_json,snapshot_hash FROM runs WHERE id=? AND project_id=?",
                                     (rep["run_id"], pid))
            if experiment is None:
                raise BizError("REPORT_EXECUTION_MISSING", "报告缺少原始实验配置绑定，只能试用", status=422)
            execution = jloads(experiment["snapshot_json"], {})
            if canonical_hash(execution) != experiment["snapshot_hash"]:
                raise BizError("REPORT_EXECUTION_MISMATCH", "实验配置校验失败，只能试用", status=422)
            if evaluator_evidence.get('source')=='model':
                from .judge_binding import admitted_for, severe_admitted_for
                judge=self.db.one('SELECT * FROM judges WHERE id=? AND project_id=?',
                                  (evaluator_evidence.get('judge_id'),pid))
                evaluation_config=execution.get('models',{}).get('evaluation') or {}
                if (not admitted_for(self.db,judge,execution.get('rubric_id'),evaluation_config)
                        or not severe_admitted_for(self.db,judge,execution.get('rubric_id'),evaluation_config)
                        or evaluator_evidence.get('binding')!=jloads(judge['metrics_json'],{}).get('evaluator_binding')):
                    raise BizError('CALIBRATION_CONTEXT_CHANGED','模型评价器或严重审计证据已失效，不能正式发布',status=409)
            verified_config = execution.get("models", {}).get("generation")
            if not verified_config or not verified_config.get("connection_snapshot"):
                raise BizError("REPORT_EXECUTION_MISSING", "旧实验未冻结执行连接，请重新验证", status=422)
            model_config = verified_config
        current = self.db.one("SELECT * FROM releases WHERE project_id=? AND status='active'", (pid,))
        if current is not None and expected_revision is not None \
                and current["revision"] != expected_revision:
            raise BizError("REVISION_CONFLICT", "已有并发发布：请刷新后重试（TC049）", status=409)
        rid = new_id("rel")
        with nullcontext(self.db._conn) as conn:
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
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._rollback(release_id,target_release_id,conn)

    def _rollback(self, release_id, target_release_id, conn):
        rel = self.get(release_id)
        target = self.get(target_release_id)
        if rel["project_id"] != target["project_id"]:
            raise BizError("NOT_FOUND", "回滚目标不属于同一项目", status=404)
        current = self.current(rel["project_id"])
        if current is None or current["id"] != release_id:
            raise BizError("REVISION_CONFLICT", "只能从当前采用记录回滚，请刷新后重试", status=409)
        if target["status"] == "trial":
            raise BizError("REPORT_NOT_VERIFIED", "试用记录不能通过回滚升级为正式采用", status=422)
        restored = self._adopt(target["project_id"], target["prompt_version_id"], target["report_ref"],
                              mode="active", expected_revision=rel["revision"])
        rid = restored["id"]
        conn.execute("UPDATE releases SET status='rolled_back' WHERE id=?", (release_id,))
        conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
            (new_id('aud'),'system','release.rollback',rid,
             canonical_hash({'previous':release_id,'target':target_release_id,'restored':rid}),now_iso()))
        return self.get(rid)

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

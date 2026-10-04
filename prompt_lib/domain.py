"""领域服务：项目、数据、评价标准、提示词、标注、评价器（D01—D05）。

业务规则以服务端为准；关键不变量：
- BR01 生成请求仅含 runtime_input 白名单，evaluation_only 永不出站（TC009）
- BR02 冻结段不可原地改写；候选改冻结段在发出任何收费请求前被拒（TC027）
- BR05 同源分组跨开发/选择/封存测试被阻止（TC008）
- BR10 评价标准/模型变化 -> 评价器 stale，历史报告不覆盖（TC024）
"""
from __future__ import annotations

import csv
import io
import json
import random

from .core import (BizError, DEFAULT_ABCD_LEVELS, RATING_SPECIAL, RESOLUTION_STATES,
                   TASK_TEMPLATES, canonical_hash, new_id, now_iso, sha256_text,
                   template_draft)
from .db import DB, get_db

MAX_TEXT_LEN = 20000
SPLITS = ("dev", "select", "sealed_test")


def jloads(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


# ---------------------------------------------------------------- 项目 P01/P02

class ProjectsService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create(self, name: str, description: str, task_type: str,
               contract: dict | None = None, goal: str = "") -> dict:
        """创建项目。任务模板仅是可选示例（优化1.0 §3.2）：用户可自带自定义契约。"""
        if not (name or "").strip():
            raise BizError("FIELD_REQUIRED", "项目名称不能为空", field_errors={"name": "必填"})
        if contract:
            from .core import normalize_custom_contract
            contract = normalize_custom_contract({**contract, "goal": goal})
            effective_type = contract["task_type"]
        else:
            draft = template_draft(task_type)
            effective_type = task_type
            contract = {
                "task_type": task_type,
                "label": draft["label"],
                "goal": goal,
                "runtime_fields": draft["runtime_fields"],
                "evaluation_fields": draft["evaluation_fields"],
                "evaluation_unit": draft["evaluation_unit"],
                "primary_metric": draft["primary_metric"],
                "acceptance_policy": {
                    "primary_metric": draft["primary_metric"],
                    "min_observed_improvement": 0.05,
                    "ci_rule": "lower_bound_gt_zero",
                    "severe_error_gate": True,
                },
                "is_demo": True,
            }
        contract["goal"] = goal or contract.get("goal", "")
        pid = new_id("prj")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,description,task_type,contract_json,status,created_at)"
                " VALUES(?,?,?,?,?,'active',?)",
                (pid, name.strip(), description or "", effective_type,
                 json.dumps(contract, ensure_ascii=False), now_iso()))
        return self.get(pid)

    def update_goal(self, pid: str, goal: str) -> dict:
        """优化目标是自由描述（§5.2），由系统辅助整理；随时可改，不影响已快照的历史运行。"""
        p = self.get(pid)
        contract = dict(p["contract"])
        contract["goal"] = (goal or "").strip()
        self.db.execute("UPDATE projects SET contract_json=? WHERE id=?",
                        (json.dumps(contract, ensure_ascii=False), pid))
        return self.get(pid)

    def get(self, pid: str) -> dict:
        row = self.db.one("SELECT * FROM projects WHERE id=?", (pid,))
        if row is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)
        return self._row(row)

    @staticmethod
    def _row(row) -> dict:
        return {"id": row["id"], "name": row["name"], "description": row["description"],
                "task_type": row["task_type"], "contract": jloads(row["contract_json"], {}),
                "status": row["status"], "created_at": row["created_at"]}

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM projects ORDER BY created_at DESC")
        out = []
        for r in rows:
            d = self._row(r)
            n = self.db.one("SELECT COUNT(*) AS c FROM runs WHERE project_id=?", (r["id"],))
            d["runs_total"] = n["c"]
            out.append(d)
        return out

    def archive(self, pid: str, archived: bool) -> dict:
        self.get(pid)
        self.db.execute("UPDATE projects SET status=? WHERE id=?",
                        ("archived" if archived else "active", pid))
        self.audit("system", "project.archive" if archived else "project.unarchive", pid)
        return self.get(pid)

    def delete(self, pid: str) -> dict:
        """硬删除项目及其全部从属数据（测试阶段能力；正式环境请先备份）。

        级联清理：案例/封存/导入批次/切分/标准/输出/标注/对比/评价器/提示词/
        运行（含账本、事件、轮次）/验收报告/发布/反馈/标签/专家意见/评级规则/案例评级。
        audit_log 为追加日志，仅保留删除事件本身。
        """
        self.get(pid)
        batch_ids = [r["id"] for r in
                     self.db.query("SELECT id FROM import_batches WHERE project_id=?", (pid,))]
        with self.db.tx() as conn:
            for run in conn.execute(
                    "SELECT id FROM runs WHERE project_id=?", (pid,)).fetchall():
                for t in ("ledger", "run_events", "run_rounds"):
                    conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run["id"],))
            for t in ("dataset_items", "sealed_artifacts", "import_batches", "split_manifests",
                      "dataset_versions", "rubrics", "outputs", "annotations", "blind_pairs",
                      "judges", "prompt_versions", "runs", "acceptance_reports", "releases",
                      "feedback", "tags", "expert_feedback", "rating_rules", "case_reviews"):
                conn.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
            for bid in batch_ids:
                conn.execute("DELETE FROM settings WHERE key=?", (f"src:{bid}",))
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.audit("system", "project.delete", pid)
        return {"deleted": pid}

    def ensure_active(self, pid: str) -> dict:
        p = self.get(pid)
        if p["status"] != "active":
            raise BizError("PROJECT_ARCHIVED",
                           "项目已归档：历史运行与报告仍可查看，但不能发起新的收费实验（TC003）",
                           status=409)
        return p

    def contract(self, pid: str) -> dict:
        return self.get(pid)["contract"]

    def audit(self, actor: str, action: str, target: str, payload_hash: str = "") -> None:
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), actor, action, target, payload_hash, now_iso()))

    def readiness(self, pid: str) -> dict:
        """准备度是依赖计算，不是AI自由判断，也不是用户手动勾选（P02）。"""
        p = self.get(pid)
        contract = p["contract"]
        dev_n = self.db.one(
            "SELECT COUNT(*) AS c FROM dataset_items WHERE project_id=? AND split IN ('dev','select')",
            (pid,))["c"]
        rubric_pub = self.db.one(
            "SELECT COUNT(*) AS c FROM rubrics WHERE project_id=? AND status='published'", (pid,))
        judge_aud = self.db.one(
            "SELECT COUNT(*) AS c FROM judges WHERE project_id=? AND status='audited' AND"
            " id NOT IN (SELECT id FROM judges WHERE status='stale')", (pid,))
        prompt_n = self.db.one(
            "SELECT COUNT(*) AS c FROM prompt_versions WHERE project_id=?", (pid,))
        manifest_n = self.db.one(
            "SELECT COUNT(*) AS c FROM split_manifests WHERE project_id=?", (pid,))
        conns = jloads(self.db.one(
            "SELECT value_json FROM settings WHERE key='connections'", )["value_json"]
            if self.db.one("SELECT value_json FROM settings WHERE key='connections'") else "[]", [])
        checks = [
            {"key": "runtime_fields", "ready": bool(contract.get("runtime_fields")),
             "page": "P01", "explain": "输入字段契约" + ("完整" if contract.get("runtime_fields") else "缺失")},
            {"key": "data", "ready": dev_n >= 1, "page": "P03",
             "explain": f"开发/选择集案例 {dev_n} 条" + ("" if dev_n >= 1 else "，请先导入并分组")},
            {"key": "rubric", "ready": rubric_pub["c"] >= 1, "page": "P04",
             "explain": "评价标准已发布" if rubric_pub["c"] >= 1 else "评价标准未发布：缺少0-3锚点或尚未发布"},
            {"key": "judge", "ready": judge_aud["c"] >= 1, "page": "P07",
             "explain": "评价器已校准发布" if judge_aud["c"] >= 1
             else "评价器未校准：可先退回人工评价继续探索，批量自动确认需要已审计评价器"},
            {"key": "prompt", "ready": prompt_n["c"] >= 1, "page": "P09",
             "explain": "已有提示词版本" if prompt_n["c"] >= 1 else "尚未创建基线提示词"},
            {"key": "manifest", "ready": manifest_n["c"] >= 1, "page": "P03",
             "explain": "分组切分已冻结" if manifest_n["c"] >= 1 else "分组切分未冻结"},
            {"key": "models", "ready": len(conns) >= 1, "page": "P14",
             "explain": "已有模型连接" if conns else "尚无模型连接（可用内置离线模拟供应商开始）"},
            {"key": "acceptance_policy", "ready": bool(contract.get("acceptance_policy")),
             "page": "P10", "explain": "验收门槛已预设" if contract.get("acceptance_policy")
             else "验收门槛未设置"},
        ]
        ready_map = {c["key"]: c["ready"] for c in checks}
        if not ready_map["data"]:
            state = "draft"
        elif not ready_map["rubric"]:
            state = "data_ready"
        elif not ready_map["judge"]:
            state = "rubric_ready"
        elif not (ready_map["prompt"] and ready_map["manifest"] and ready_map["models"]
                  and ready_map["acceptance_policy"]):
            state = "judge_ready"
        else:
            state = "experiment_ready"

        # ---- 07 方案 R01/R05/§12：按动作计算资格（替代单一线性准备度） ----
        # 每个动作独立判断：可做 / 需补齐（原因+修复入口）；种子输出不要求评价器（R01/TC061）
        outputs_n = self.db.one("SELECT COUNT(*) AS c FROM outputs WHERE project_id=?", (pid,))["c"]
        locked_run = self.db.one(
            "SELECT id FROM runs WHERE project_id=? AND locked_candidate!='' AND locked_candidate"
            " IS NOT NULL ORDER BY created_at DESC LIMIT 1", (pid,))
        verified = self.db.one(
            "SELECT id,decision FROM acceptance_reports WHERE project_id=?"
            " ORDER BY created_at DESC LIMIT 1", (pid,))
        sealed_n = self.db.one(
            "SELECT COUNT(*) AS c FROM sealed_artifacts WHERE project_id=?"
            " AND access_state='sealed'", (pid,))["c"]

        def act(name, ok, reasons, fix_page):
            return {"action": name, "status": "可做" if ok else "需补齐",
                    "reasons": reasons, "fix_entry": fix_page}

        actions = []
        if ready_map["runtime_fields"] and ready_map["models"] and prompt_n["c"] >= 1:
            actions.append(act("生成种子输出", True,
                               ["输入契约可渲染、模型连接有效（内置演示供应商即可）"], "P03"))
        else:
            miss = []
            if prompt_n["c"] < 1:
                miss.append("还没有提示词原文")
            if not ready_map["models"]:
                miss.append("没有可用模型连接")
            actions.append(act("生成种子输出", False, miss or ["输入契约缺失"], "P03"))
        if outputs_n >= 1 and ready_map["rubric"]:
            actions.append(act("人工比较一个候选", True,
                               ["已有实际输出与已发布评价标准；未校准评价器也可人工判断（R02）"], "P06"))
        else:
            miss = []
            if outputs_n < 1:
                miss.append("还没有实际输出：先运行一次原始测评或试运行")
            if not ready_map["rubric"]:
                miss.append("评价标准未发布")
            actions.append(act("人工比较一个候选", False, miss, "P04"))
        if ready_map["data"] and ready_map["manifest"] and ready_map["rubric"] and ready_map["models"]:
            actions.append(act("自动批量搜索", True,
                               ["开发/选择集已冻结、评价计划已确认、预算受账本约束；"
                                + ("未校准评价器仅限探索模式（结果不用于自动确认）"
                                   if not ready_map["judge"] else "评价器已审计")], "P10"))
        else:
            miss = []
            if not ready_map["data"]:
                miss.append("开发集为空")
            if not ready_map["manifest"]:
                miss.append("分组未冻结")
            if not ready_map["rubric"]:
                miss.append("评价标准未发布")
            actions.append(act("自动批量搜索", False, miss, "P03"))
        if sealed_n >= 1 and locked_run is not None:
            actions.append(act("正式验证（独立验证）", True,
                               [f"候选已锁定（运行 {locked_run['id'][:16]}…）；考题 {sealed_n} 条已封存，"
                                "解封一次性消耗（一次绑定）"], "P12"))
        else:
            miss = []
            if sealed_n < 1:
                miss.append("没有已封存的考题（锁定分组时自动划分）")
            if locked_run is None:
                miss.append("还没有锁定候选的运行（在运行详情中锁定）")
            actions.append(act("正式验证（独立验证）", False, miss, "P11"))
        # 正式采用资格以报告的四层结论为准（与 eligibility 一致，避免页面互相矛盾）
        if verified is not None and verified["decision"] == "verified_improvement":
            erow = self.db.one("SELECT eligibility FROM acceptance_reports WHERE id=?",
                               (verified["id"],))
            elig = (erow["eligibility"] or "") if erow else ""
            if "可正式采用" in elig:
                actions.append(act("正式采用", True,
                                   [f"验证有效报告（{verified['id']}）全部门槛通过，确认后更新正式指针"],
                                   "P13"))
            else:
                actions.append(act("正式采用", False,
                                   [f"验证有效报告（{verified['id']}）存在，但 {elig or '门槛未全通过'}"],
                                   "P12"))
        else:
            actions.append(act("正式采用", False,
                               ["还没有“验证有效”的独立验证报告；未验证候选只能保存为试用版本"], "P12"))

        return {"state": state, "checks": checks, "actions": actions}

    def progress(self, pid: str) -> dict:
        """五步流程进度（优化1.0 §4.2）：每步为何要做、当前状态与下一步主行动。"""
        p = self.get(pid)
        dev_n = self.db.one(
            "SELECT COUNT(*) AS c FROM dataset_items WHERE project_id=? AND split IN ('dev','select')",
            (pid,))["c"]
        prompt_n = self.db.one("SELECT COUNT(*) AS c FROM prompt_versions WHERE project_id=?", (pid,))
        rub_pub = self.db.one(
            "SELECT COUNT(*) AS c FROM rubrics WHERE project_id=? AND status='published'", (pid,))
        runs = self.db.query("SELECT * FROM runs WHERE project_id=?", (pid,))
        baseline_done = any(r["baseline_detail_json"] and r["baseline_detail_json"] != "{}"
                            for r in runs)
        kept_any = any("kept" in (r["candidates_json"] or "") for r in runs)
        reports_n = self.db.one(
            "SELECT COUNT(*) AS c FROM acceptance_reports WHERE project_id=?", (pid,))["c"]
        steps = [
            {"key": "prepare", "name": "准备材料",
             "why": "把你的提示词和真实案例交给系统，它才知道要优化什么、实际会遇到哪些情况。",
             "then": "系统检查材料并标出每个字段的用途；案例就绪后进入下一步。",
             "done": prompt_n["c"] >= 1 and dev_n >= 1,
             "explain": f"提示词版本 {prompt_n['c']} 个，可用案例 {dev_n} 条",
             "action": "去准备提示词和案例" if prompt_n["c"] < 1 or dev_n < 1 else "查看已准备的材料"},
            {"key": "confirm_eval", "name": "确认怎么评",
             "why": "先说好“怎样算改好了”，系统才能自动判断改得有没有效——标准由你确认，系统不会私自更改。",
             "then": "生成一份运行摘要；确认后就能开始原始测评。",
             "done": rub_pub["c"] >= 1,
             "explain": "评价标准已发布" if rub_pub["c"] >= 1 else "还没有约定评价标准",
             "action": "去确认评价方式" if rub_pub["c"] < 1 else "查看评价方式"},
            {"key": "baseline", "name": "原始测评",
             "why": "先测一遍现在的提示词，看清它差在哪——这是后面所有比较的起点。",
             "then": "得到一份带证据的问题清单；确认这些问题确实是你想解决的。",
             "done": baseline_done,
             "explain": "已完成原始测评" if baseline_done else "还没有测过现状",
             "action": "去测现状" if not baseline_done else "查看问题清单"},
            {"key": "optimize", "name": "自动优化",
             "why": "系统反复“改一点→测一遍”，只保留真的变好的版本；原来会的不能变差。",
             "then": "每轮都有“为什么改、改了什么、效果如何”的记录；结束后锁定待验证的版本。",
             "done": kept_any,
             "explain": "已有保留的优化候选" if kept_any else "还没有产生保留的改写",
             "action": "开始自动优化" if not kept_any else "查看优化过程"},
            {"key": "verify", "name": "验证与使用",
             "why": "用没参与过修改的新案例做最终检验，防止只是“背会了练习题”。",
             "then": "得到明确结论（有效/未见提升/退步/证据不足）和报告；通过后拿走新提示词。",
             "done": reports_n >= 1,
             "explain": f"独立验证报告 {reports_n} 份" if reports_n else "还没有做过独立验证",
             "action": "去做最终检验" if not reports_n else "查看结果报告"},
        ]
        current = next((s for s in steps if not s["done"]), steps[-1])
        return {"project": p, "steps": steps, "current": current["key"],
                "next_action": current["action"]}


# ---------------------------------------------------------------- 数据 P03

class DataService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    # ---- 导入预览（不写正式资产；逐行错误，不静默丢行 TC006）
    def preview_import(self, pid: str, source_name: str, fmt: str, content: str,
                       field_map: dict | None = None) -> dict:
        self._project(pid)
        contract = self._contract(pid)
        rows, errors = [], []
        if fmt == "jsonl":
            for i, line in enumerate(content.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append((i, json.loads(line)))
                except Exception as e:
                    errors.append({"line": i, "reason": f"JSON解析失败：{e}"})
        elif fmt == "csv":
            reader = csv.DictReader(io.StringIO(content))
            mapping = field_map or {}
            for i, raw in enumerate(reader, 2):
                row = {}
                for col, val in raw.items():
                    if col is None:
                        continue
                    key = mapping.get(col, col)
                    if key in ("case_id", "source_group_id", "origin", "scene_tags"):
                        row[key] = val
                    elif key.startswith("runtime:"):
                        row.setdefault("runtime_input", {})[key.split(":", 1)[1]] = val
                    elif key.startswith("eval:"):
                        row.setdefault("evaluation_only", {})[key.split(":", 1)[1]] = val
                    else:
                        row.setdefault("runtime_input", {})[key] = val
                rows.append((i, row))
        else:
            raise BizError("FORMAT_UNKNOWN", "格式只支持 jsonl / csv")

        runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
        required = [f["name"] for f in contract.get("runtime_fields", []) if f.get("required", True)]
        seen_ids: set = set()
        valid = []
        for i, row in rows:
            errs = []
            case_id = str(row.get("case_id") or "").strip()
            if not case_id:
                errs.append("缺少 case_id")
            elif case_id in seen_ids:
                errs.append(f"重复ID：{case_id}")
            ri = row.get("runtime_input") or {}
            eo = row.get("evaluation_only") or {}
            for f in required:
                if not str(ri.get(f) or "").strip():
                    errs.append(f"缺少必填运行时字段：{f}")
            for f in ri:
                if f not in runtime_names:
                    errs.append(f"未知运行时字段：{f}（不在契约白名单）")
            for v in list(ri.values()) + list(eo.values()):
                if isinstance(v, str) and len(v) > MAX_TEXT_LEN:
                    errs.append(f"文本超限（>{MAX_TEXT_LEN}字）")
            origin = row.get("origin") or "real"
            if origin not in ("real", "synthetic"):
                errs.append(f"origin 非法：{origin}（AI合成必须显式标记为synthetic）")
            if errs:
                errors.append({"line": i, "case_id": case_id, "reasons": errs})
            else:
                seen_ids.add(case_id)
                valid.append({"case_id": case_id,
                              "source_group_id": str(row.get("source_group_id") or case_id),
                              "origin": origin,
                              "scene_tags": row.get("scene_tags") or "",
                              "runtime_input": ri, "evaluation_only": eo})
        content_hash = sha256_text(content)
        preview_hash = canonical_hash({"project": pid, "content_hash": content_hash,
                                       "field_map": field_map or {}, "n_valid": len(valid)})
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM import_batches WHERE project_id=? AND preview_hash=?",
                (pid, preview_hash)).fetchone()
            if existing:
                return self._batch(existing)
            bid = new_id("imp")
            conn.execute(
                "INSERT INTO import_batches(id,project_id,preview_hash,source_name,total,valid,"
                "errors_json,excluded_json,committed,created_at) VALUES(?,?,?,?,?,?,?,?,'0',?)",
                (bid, pid, preview_hash, source_name, len(rows), len(valid),
                 json.dumps(errors, ensure_ascii=False), json.dumps([], ensure_ascii=False),
                 now_iso()))
        return self.get_batch(bid)

    def get_batch(self, bid: str) -> dict:
        row = self.db.one("SELECT * FROM import_batches WHERE id=?", (bid,))
        if row is None:
            raise BizError("NOT_FOUND", "导入批次不存在", status=404)
        return self._batch(row)

    @staticmethod
    def _batch(row) -> dict:
        return {"id": row["id"], "project_id": row["project_id"], "preview_hash": row["preview_hash"],
                "source_name": row["source_name"], "total": row["total"], "valid": row["valid"],
                "errors": jloads(row["errors_json"], []), "committed": bool(row["committed"]),
                "created_at": row["created_at"]}

    # ---- 提交（幂等：同 preview_hash 返回原批次；源内容变化则 hash 不匹配被拒 TC007）
    def commit_import(self, pid: str, batch_id: str, exclude_case_ids: list[str] | None = None) -> dict:
        self._project(pid)
        batch = self.get_batch(batch_id)
        if batch["project_id"] != pid:
            raise BizError("NOT_FOUND", "批次与项目不匹配", status=404)
        if batch["committed"]:
            return batch  # 幂等返回
        exclude = set(exclude_case_ids or [])
        # 预览后修改源文件 -> content hash 变化 -> 拒绝提交
        src = self.db.one("SELECT value_json FROM settings WHERE key=?", (f"src:{batch_id}",))
        if src is not None and jloads(src["value_json"], "") != batch["preview_hash"]:
            pass  # hash 存储在 preview_hash 本身，见 preview_import
        contract = self._contract(pid)
        runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
        version = self._next_version(pid, "dataset_versions")
        vid = new_id("dsv")
        kept, excluded_rows = [], []
        # 重新校验排除清单必须来自有效行
        with self.db.tx() as conn:
            conn.execute("UPDATE import_batches SET committed=1 WHERE id=?", (batch_id,))
            for item in self._batch_valid_items(batch_id):
                if item["case_id"] in exclude:
                    excluded_rows.append(item["case_id"])
                    continue
                iid = new_id("case")
                ri = {k: v for k, v in item["runtime_input"].items() if k in runtime_names}
                eo = item["evaluation_only"]
                ch = canonical_hash({"ri": ri, "eo": eo, "case": item["case_id"]})
                conn.execute(
                    "INSERT INTO dataset_items(id,project_id,case_id,source_group_id,origin,"
                    "runtime_input_json,evaluation_only_json,split,scene_tags,eval_status,"
                    "content_hash,version_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,'unassigned',?, 'none', ?, ?, ?)",
                    (iid, pid, item["case_id"], item["source_group_id"], item["origin"],
                     json.dumps(ri, ensure_ascii=False), json.dumps(eo, ensure_ascii=False),
                     item["scene_tags"], ch, vid, now_iso()))
                kept.append(iid)
            conn.execute(
                "INSERT INTO dataset_versions(id,project_id,version_no,note,snapshot_json,hash,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (vid, pid, version, f"导入批次 {batch_id}",
                 json.dumps({"item_ids": kept, "excluded": excluded_rows}, ensure_ascii=False),
                 canonical_hash({"item_ids": kept}), now_iso()))
            conn.execute("UPDATE import_batches SET excluded_json=? WHERE id=?",
                         (json.dumps(excluded_rows, ensure_ascii=False), batch_id))
        self._audit_import(pid, batch_id, len(kept))
        return self.get_batch(batch_id)

    def _batch_valid_items(self, batch_id: str) -> list[dict]:
        """重新解析源内容（保存在受限区 settings，演示实现）。"""
        src = self.db.one("SELECT value_json FROM settings WHERE key=?", (f"src:{batch_id}",))
        if src is None:
            return []
        payload = jloads(src["value_json"], {})
        if payload.get("preview_hash") != self.get_batch(batch_id)["preview_hash"]:
            raise BizError("PREVIEW_HASH_MISMATCH",
                           "预览后源内容已变化，提交被拒绝；请重新预览（TC007）", status=409)
        return payload.get("valid", [])

    def store_source(self, batch_id: str, preview_hash: str, valid_items: list[dict]) -> None:
        """把预览的有效行保存到受限区，提交时复核 hash。"""
        self.db.execute(
            "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            (f"src:{batch_id}", json.dumps({"preview_hash": preview_hash, "valid": valid_items},
                                           ensure_ascii=False), now_iso()))

    def _audit_import(self, pid: str, batch_id: str, n: int) -> None:
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", "import.commit", f"{pid}/{batch_id}",
                         sha256_text(str(n)), now_iso()))

    # ---- 列表：封存测试原文不出现在普通列表（TC010）
    def list_items(self, pid: str, split: str | None = None, page: int = 1, size: int = 50) -> dict:
        self._project(pid)
        cond, params = ["project_id=?"], [pid]
        if split:
            cond.append("split=?")
            params.append(split)
        if split == "sealed_test":
            sealed_n = self.db.one(
                "SELECT COUNT(*) AS c FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'",
                (pid,))["c"]
            return {"items": [], "total": 0, "sealed_summary": {
                "count": sealed_n,
                "note": "封存测试原文受限：普通列表只能看到数量与覆盖，解封后仅供验收任务访问（TC010）"}}
        total = self.db.one(f"SELECT COUNT(*) AS c FROM dataset_items WHERE {' AND '.join(cond)}",
                            tuple(params))["c"]
        rows = self.db.query(
            f"SELECT * FROM dataset_items WHERE {' AND '.join(cond)}"
            f" ORDER BY case_id LIMIT ? OFFSET ?", tuple(params + [size, (page - 1) * size]))
        items = []
        for r in rows:
            items.append({"id": r["id"], "case_id": r["case_id"],
                          "source_group_id": r["source_group_id"], "origin": r["origin"],
                          "split": r["split"], "scene_tags": r["scene_tags"],
                          "eval_status": r["eval_status"],
                          "runtime_input": jloads(r["runtime_input_json"], {}),
                          "content_hash": r["content_hash"], "created_at": r["created_at"]})
        return {"items": items, "total": total, "page": page, "size": size}

    def get_item_runtime(self, pid: str, item_id: str) -> dict:
        """生成/评价使用的视图：只含 runtime_input，绝不含 evaluation_only（BR01/TC009）。"""
        r = self.db.one("SELECT * FROM dataset_items WHERE id=? AND project_id=?", (item_id, pid))
        if r is None:
            raise BizError("NOT_FOUND", "案例不存在", status=404)
        return {"id": r["id"], "case_id": r["case_id"], "split": r["split"],
                "runtime_input": jloads(r["runtime_input_json"], {}),
                "source_group_id": r["source_group_id"]}

    def get_item_full(self, item_id: str) -> dict:
        r = self.db.one("SELECT * FROM dataset_items WHERE id=?", (item_id,))
        if r is None:
            raise BizError("NOT_FOUND", "案例不存在", status=404)
        return {"id": r["id"], "case_id": r["case_id"], "split": r["split"],
                "runtime_input": jloads(r["runtime_input_json"], {}),
                "evaluation_only": jloads(r["evaluation_only_json"], {}),
                "source_group_id": r["source_group_id"]}

    def set_groups(self, pid: str, assignments: dict) -> dict:
        """case_id -> source_group_id 批量设置。"""
        self._project(pid)
        with self.db.tx() as conn:
            for case_id, group in assignments.items():
                conn.execute("UPDATE dataset_items SET source_group_id=? WHERE project_id=? AND case_id=?",
                             (str(group), pid, case_id))
        return {"updated": len(assignments)}

    def assign_split(self, pid: str, case_ids: list[str], split: str) -> dict:
        if split not in SPLITS + ("unassigned",):
            raise BizError("SPLIT_UNKNOWN", f"未知集合：{split}")
        with self.db.tx() as conn:
            for cid in case_ids:
                conn.execute(
                    "UPDATE dataset_items SET split=? WHERE project_id=? AND (case_id=? OR id=?)",
                    (split, pid, cid, cid))
        return {"updated": len(case_ids)}

    # ---- 冻结切分：同源跨集合泄漏被阻止（TC008）；封存原文进受限区（TC010）
    def freeze_manifest(self, pid: str, seed: int = 20260927,
                        weights: dict | None = None) -> dict:
        self._project(pid)
        rows = self.db.query(
            "SELECT * FROM dataset_items WHERE project_id=? AND split IN ('dev','select','sealed_test')",
            (pid,))
        if not rows:
            raise BizError("NO_DATA", "没有可冻结的案例：请先导入并分配 dev/select/sealed_test")
        group_map: dict[str, set] = {}
        for r in rows:
            group_map.setdefault(r["source_group_id"], set()).add(r["split"])
        leaked = {g: sorted(s) for g, s in group_map.items() if len(s) > 1}
        if leaked:
            raise BizError("DATA_SPLIT_LEAKAGE",
                           "存在同源分组跨集合：同一 source_group 必须落在同一集合（TC008）",
                           field_errors={"leaked_groups": leaked}, status=422)
        group_list = {r["case_id"]: r["source_group_id"] for r in rows}
        gm_hash = canonical_hash(group_list)
        vid = self._snapshot_version(pid, rows, note="冻结切分")
        mid = new_id("man")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO split_manifests(id,project_id,dataset_version_id,group_map_hash,seed,"
                "weights_json,state,created_at) VALUES(?,?,?,?,?,?, 'frozen', ?)",
                (mid, pid, vid, gm_hash, seed, json.dumps(weights or {}, ensure_ascii=False), now_iso()))
            for r in rows:
                if r["split"] == "sealed_test":
                    conn.execute(
                        "INSERT OR REPLACE INTO sealed_artifacts(item_id,project_id,sealed_json,sha256,"
                        "access_state,created_at) VALUES(?,?,?,?, 'sealed', ?)",
                        (r["id"], pid,
                         json.dumps({"runtime_input": jloads(r["runtime_input_json"], {}),
                                     "evaluation_only": jloads(r["evaluation_only_json"], {})},
                                    ensure_ascii=False),
                         r["content_hash"], now_iso()))
                    # 原文从通用表中移除：普通选择器无法读取（TC010）
                    conn.execute("UPDATE dataset_items SET runtime_input_json='{}',"
                                 " evaluation_only_json='{}' WHERE id=?", (r["id"],))
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", "manifest.freeze", mid, gm_hash, now_iso()))
        return {"manifest_id": mid, "dataset_version_id": vid, "groups": len(group_map),
                "sealed_test_items": sum(1 for r in rows if r["split"] == "sealed_test")}

    def _snapshot_version(self, pid: str, rows, note: str) -> str:
        version = self._next_version(pid, "dataset_versions")
        vid = new_id("dsv")
        item_ids = [r["id"] for r in rows]
        self.db.execute(
            "INSERT INTO dataset_versions(id,project_id,version_no,note,snapshot_json,hash,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (vid, pid, version, note, json.dumps({"item_ids": item_ids}, ensure_ascii=False),
             canonical_hash({"item_ids": item_ids}), now_iso()))
        with self.db.tx() as conn:
            for iid in item_ids:
                conn.execute("UPDATE dataset_items SET version_id=? WHERE id=?", (vid, iid))
        return vid

    def _next_version(self, pid: str, table: str) -> int:
        row = self.db.one(f"SELECT COALESCE(MAX(version_no),0)+1 AS n FROM {table} WHERE project_id=?",
                          (pid,))
        return row["n"]

    def unseal_for_acceptance(self, pid: str) -> list[dict]:
        """验收任务专用：解封并返回原文，同时把状态改为 consumed（一次性，TC043）。"""
        rows = self.db.query(
            "SELECT * FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'", (pid,))
        out = []
        with self.db.tx() as conn:
            for r in rows:
                payload = json.loads(r["sealed_json"])
                out.append({"item_id": r["item_id"], "sha256": r["sha256"], **payload})
                conn.execute("UPDATE sealed_artifacts SET access_state='consumed' WHERE item_id=?",
                             (r["item_id"],))
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", "sealed.unseal_for_acceptance", pid,
                         sha256_text(str(len(rows))), now_iso()))
        return out

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)

    def _contract(self, pid: str) -> dict:
        row = self.db.one("SELECT contract_json FROM projects WHERE id=?", (pid,))
        return json.loads(row["contract_json"])


# ---------------------------------------------------------------- 评价标准 P04

class RubricService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create_draft(self, pid: str) -> dict:
        contract = json.loads(self.db.one("SELECT contract_json FROM projects WHERE id=?",
                                          (pid,))["contract_json"])
        task = contract["task_type"]
        if task in TASK_TEMPLATES:
            t = TASK_TEMPLATES[task]
            schema = {"dimensions": t["dimensions"], "error_tags": [], "hard_rules": [],
                      "severity_examples": t["severity_examples"], "weights": {}}
        else:
            # 自定义任务：生成可编辑的通用草案（§6.2 系统建议，需人工确认）；
            # 契约自带 dimensions 时优先使用
            dims = contract.get("dimensions") or [
                {"name": "整体可用", "anchors": {"0": "输出完全不可用，需要重做",
                                                 "1": "方向正确但需大幅重写",
                                                 "2": "小改后可用",
                                                 "3": "可直接使用"}},
                {"name": "符合要求", "anchors": {"0": "违反任务硬性约束",
                                                 "1": "多处不符合要求",
                                                 "2": "基本符合要求",
                                                 "3": "完全符合且处理了边界情况"}},
            ]
            schema = {"dimensions": dims, "error_tags": [], "hard_rules": [],
                      "severity_examples": contract.get("severity_examples") or [],
                      "weights": {}}
        return self._insert(pid, schema, status="draft")

    def _insert(self, pid: str, schema: dict, status: str) -> dict:
        rid = new_id("rub")
        row = self.db.one("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM rubrics WHERE project_id=?",
                          (pid,))
        h = canonical_hash(schema)
        self.db.execute(
            "INSERT INTO rubrics(id,project_id,version_no,status,schema_json,hash,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (rid, pid, row["n"], status, json.dumps(schema, ensure_ascii=False), h, now_iso()))
        return self.get(rid)

    def get(self, rid: str) -> dict:
        r = self.db.one("SELECT * FROM rubrics WHERE id=?", (rid,))
        if r is None:
            raise BizError("NOT_FOUND", "评价标准不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"], "version_no": r["version_no"],
                "status": r["status"], "schema": jloads(r["schema_json"], {}),
                "hash": r["hash"], "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in
                self.db.query("SELECT id FROM rubrics WHERE project_id=? ORDER BY version_no", (pid,))]

    def update_draft(self, rid: str, schema: dict) -> dict:
        cur = self.get(rid)
        if cur["status"] != "draft":
            raise BizError("RUBRIC_IMMUTABLE",
                           "已发布版本不可覆盖修改（BR02）；修改会产生新版本", status=409)
        h = canonical_hash(schema)
        self.db.execute("UPDATE rubrics SET schema_json=?, hash=? WHERE id=?",
                        (json.dumps(schema, ensure_ascii=False), h, rid))
        return self.get(rid)

    def publish(self, rid: str) -> dict:
        cur = self.get(rid)
        schema = cur["schema"]
        # 缺锚点/权重非法 -> 定位到具体维度（TC012）
        field_errors = {}
        for i, dim in enumerate(schema.get("dimensions", [])):
            anchors = dim.get("anchors") or {}
            missing = [k for k in ("0", "1", "2", "3") if not str(anchors.get(k) or "").strip()]
            if missing:
                field_errors[f"dimensions[{i}].{dim.get('name')}"] = f"缺少锚点档位：{missing}"
        for i, w in (schema.get("weights") or {}).items():
            try:
                if float(w) < 0:
                    field_errors[f"weights.{w}"] = "权重不能为负"
            except (TypeError, ValueError):
                field_errors[f"weights.{w}"] = "权重必须是数字"
        if field_errors:
            raise BizError("RUBRIC_INVALID", "评价标准存在缺锚点或非法权重，无法发布（TC012）",
                           field_errors=field_errors)
        self.db.execute("UPDATE rubrics SET status='published' WHERE id=?", (rid,))
        # 标准变化 -> 旧评价器 stale（TC024）；历史报告保留不覆盖
        self.db.execute("UPDATE judges SET status='stale' WHERE project_id=? AND status IN"
                        " ('draft','calibrating','audited')", (cur["project_id"],))
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", "rubric.publish", rid, cur["hash"], now_iso()))
        return self.get(rid)


# ---------------------------------------------------------------- 提示词 P08/P09

class PromptService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create_version(self, pid: str, name: str, body: str, frozen_segments: list[dict] | None,
                       variables: list[str] | None, params: dict | None,
                       parent_id: str = "", origin: str = "manual",
                       hypothesis: str = "") -> dict:
        self._project(pid)
        contract = self._contract(pid)
        runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
        bad_vars = [v for v in (variables or []) if v not in runtime_names]
        if bad_vars:
            raise BizError("VARIABLE_NOT_ALLOWED",
                           f"模板变量必须在运行时白名单内：非法 {bad_vars}（BR01/TC027）")
        row = self.db.one("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM prompt_versions"
                          " WHERE project_id=? AND name=?", (pid, name))
        h = canonical_hash({"body": body, "frozen": frozen_segments or [],
                            "variables": variables or [], "params": params or {}})
        pvid = new_id("pv")
        self.db.execute(
            "INSERT INTO prompt_versions(id,project_id,name,version_no,parent_id,frozen_segments_json,"
            "body,variables_json,params_json,hash,origin,hypothesis,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pvid, pid, name, row["n"], parent_id,
             json.dumps(frozen_segments or [], ensure_ascii=False), body,
             json.dumps(variables or [], ensure_ascii=False),
             json.dumps(params or {}, ensure_ascii=False), h, origin, hypothesis, now_iso()))
        return self.get(pvid)

    def get(self, pvid: str) -> dict:
        r = self.db.one("SELECT * FROM prompt_versions WHERE id=?", (pvid,))
        if r is None:
            raise BizError("NOT_FOUND", "提示词版本不存在", status=404)
        return self._row(r)

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "project_id": r["project_id"], "name": r["name"],
                "version_no": r["version_no"], "parent_id": r["parent_id"],
                "frozen_segments": jloads(r["frozen_segments_json"], []), "body": r["body"],
                "variables": jloads(r["variables_json"], []),
                "params": jloads(r["params_json"], {}), "hash": r["hash"],
                "origin": r["origin"], "hypothesis": r["hypothesis"],
                "length": len(r["body"] or ""),
                "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self._row(r) for r in self.db.query(
            "SELECT * FROM prompt_versions WHERE project_id=? ORDER BY name, version_no", (pid,))]

    def validate_candidate(self, base: dict, candidate: dict) -> None:
        """冻结段与变量白名单校验：在任何收费请求发出前调用（TC027）。"""
        bf = {s.get("name"): s.get("text") for s in base.get("frozen_segments", [])}
        cf = {s.get("name"): s.get("text") for s in candidate.get("frozen_segments", [])}
        changed = [k for k in bf if k in cf and bf[k] != cf[k]]
        added = [k for k in cf if k not in bf]
        if changed or added:
            raise BizError("FROZEN_COMPONENT_CHANGED",
                           f"候选修改了冻结组件：{changed or added}。冻结段由服务端拼接，不可改写"
                           "（BR02/TC027），未发出任何收费请求")
        contract = self._contract(candidate["project_id"])
        runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
        bad = [v for v in candidate.get("variables", []) if v not in runtime_names]
        if bad:
            raise BizError("VARIABLE_NOT_ALLOWED", f"候选新增了白名单外变量：{bad}")

    def diff(self, a_id: str, b_id: str) -> dict:
        a, b = self.get(a_id), self.get(b_id)
        return {"a": {"id": a["id"], "version_no": a["version_no"]},
                "b": {"id": b["id"], "version_no": b["version_no"]},
                "body_changed": a["body"] != b["body"],
                "params_changed": a["params"] != b["params"],
                "frozen_changed": a["frozen_segments"] != b["frozen_segments"],
                "a_body": a["body"], "b_body": b["body"]}

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)

    def _contract(self, pid: str) -> dict:
        return json.loads(self.db.one("SELECT contract_json FROM projects WHERE id=?",
                                      (pid,))["contract_json"])


# ---------------------------------------------------------------- 标注与匿名对比 P05/P06

class AnnotationService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create_pair(self, pid: str, left_output_id: str, right_output_id: str,
                    purpose: str) -> dict:
        self._project(pid)
        for oid in (left_output_id, right_output_id):
            if self.db.one("SELECT 1 FROM outputs WHERE id=?", (oid,)) is None:
                raise BizError("NOT_FOUND", f"输出不存在：{oid}", status=404)
        pub = "pair_" + new_id("x")[2:12]
        self.db.execute(
            "INSERT INTO blind_pairs(id,project_id,public_id,left_output_id,right_output_id,"
            "order_seed,purpose,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("bp"), pid, pub, left_output_id, right_output_id,
             _fingerprint_seed_int(pub), purpose, now_iso()))
        return self.get_pair_owner(pub)

    def get_pair_owner(self, public_id: str) -> dict:
        r = self.db.one("SELECT * FROM blind_pairs WHERE public_id=?", (public_id,))
        if r is None:
            raise BizError("NOT_FOUND", "对比不存在", status=404)
        return {"id": r["id"], "public_id": r["public_id"], "project_id": r["project_id"],
                "left_output_id": r["left_output_id"], "right_output_id": r["right_output_id"],
                "purpose": r["purpose"], "revealed": bool(r["revealed"])}

    def get_pair_blind(self, public_id: str) -> dict:
        """盲评视图：不返回版本ID、名称、时间、评分或搜索分（TC018）。"""
        r = self.db.one("SELECT * FROM blind_pairs WHERE public_id=?", (public_id,))
        if r is None:
            raise BizError("NOT_FOUND", "对比不存在", status=404)
        left = self.db.one("SELECT text FROM outputs WHERE id=?", (r["left_output_id"],))
        right = self.db.one("SELECT text FROM outputs WHERE id=?", (r["right_output_id"],))
        return {"public_id": r["public_id"], "purpose": r["purpose"],
                "left": {"text": left["text"] if left else ""},
                "right": {"text": right["text"] if right else ""}}

    def reveal(self, public_id: str) -> dict:
        p = self.get_pair_owner(public_id)
        self.db.execute("UPDATE blind_pairs SET revealed=1 WHERE public_id=?", (public_id,))
        return p

    def submit_annotation(self, public_id: str, payload: dict, annotator: str = "owner") -> dict:
        p = self.get_pair_owner(public_id)
        rubric = self._published_rubric(p["project_id"])
        pref = payload.get("preference")
        if pref not in ("A", "B", "tie", "both_unusable", "unknown"):
            raise BizError("PREFERENCE_INVALID",
                           "偏好必须是 A/B/tie/both_unusable/unknown；两边都不可用不能强选胜者（TC019）")
        dims = [d["name"] for d in rubric["schema"].get("dimensions", [])]
        scores = {}
        for k, v in (payload.get("scores") or {}).items():
            if k not in dims:
                raise BizError("SCORE_DIM_UNKNOWN", f"评分维度不在已发布标准中：{k}")
            if v in ("not_applicable", "unknown"):
                scores[k] = v
            elif isinstance(v, int) and 0 <= v <= 3:
                scores[k] = v
            else:
                raise BizError("SCORE_INVALID", f"维度 {k} 的分值必须是 0-3/not_applicable/unknown")
        evidence = []
        texts = {"left": self.db.one("SELECT text FROM outputs WHERE id=?",
                                     (p["left_output_id"],))["text"],
                 "right": self.db.one("SELECT text FROM outputs WHERE id=?",
                                      (p["right_output_id"],))["text"]}
        for ev in payload.get("evidence", []):
            side = ev.get("side")
            if side not in texts:
                raise BizError("EVIDENCE_SIDE_INVALID", "证据 side 必须是 left/right")
            text = texts[side]
            start, end = int(ev.get("start", 0)), int(ev.get("end", 0))
            quote = ev.get("quote", "")
            # Unicode 码点位置校验：错位拒绝（TC017）；前端负责 UTF-16 转换
            if not (0 <= start <= end <= len(text)) or text[start:end] != quote:
                raise BizError("EVIDENCE_MISMATCH",
                               f"证据引用与码点位置不一致：[{start}:{end}] != quote（TC017）")
            evidence.append({"side": side, "start": start, "end": end, "quote": quote,
                             "output_hash": sha256_text(text)})
        aid = new_id("ann")
        self.db.execute(
            "INSERT INTO annotations(id,project_id,output_id,rubric_id,source,gold_status,"
            "scores_json,evidence_json,pair_public_id,side,purpose,annotator,submitted,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?, '1', ?)",
            (aid, p["project_id"], p["left_output_id"] if pref == "A" else p["right_output_id"],
             rubric["id"], "human", "human_verified", json.dumps(
                 {"preference": pref, "scores": scores}, ensure_ascii=False),
             json.dumps(evidence, ensure_ascii=False), public_id,
             pref if pref in ("A", "B") else "", p["purpose"], annotator, now_iso()))
        return {"id": aid, "pair_public_id": public_id, "preference": pref}

    def model_pre_annotation(self, pid: str, output_id: str, rubric_id: str, scores: dict) -> dict:
        """模型预标注：进入 gold 前必须人工核验（TC020）。"""
        aid = new_id("ann")
        self.db.execute(
            "INSERT INTO annotations(id,project_id,output_id,rubric_id,source,gold_status,"
            "scores_json,evidence_json,created_at) VALUES(?,?,?,?,'model_pre','model_pre',?,?,?)",
            (aid, pid, output_id, rubric_id, json.dumps({"scores": scores}, ensure_ascii=False),
             "[]", now_iso()))
        return {"id": aid, "gold_status": "model_pre"}

    def verify_annotation(self, annotation_id: str) -> dict:
        row = self.db.one("SELECT * FROM annotations WHERE id=?", (annotation_id,))
        if row is None:
            raise BizError("NOT_FOUND", "标注不存在", status=404)
        if row["source"] != "model_pre":
            return {"id": annotation_id, "gold_status": row["gold_status"]}
        self.db.execute("UPDATE annotations SET gold_status='human_verified' WHERE id=?",
                        (annotation_id,))
        return {"id": annotation_id, "gold_status": "human_verified"}

    def list_annotations(self, pid: str, gold_only: bool = False) -> list[dict]:
        cond = "project_id=?" + (" AND gold_status IN ('human_verified','adjudicated')" if gold_only else "")
        rows = self.db.query(f"SELECT * FROM annotations WHERE {cond}", (pid,))
        return [{"id": r["id"], "output_id": r["output_id"], "rubric_id": r["rubric_id"],
                 "source": r["source"], "gold_status": r["gold_status"],
                 "payload": jloads(r["scores_json"], {}), "purpose": r["purpose"]} for r in rows]

    def _published_rubric(self, pid: str) -> dict:
        r = self.db.one("SELECT * FROM rubrics WHERE project_id=? AND status='published'"
                        " ORDER BY version_no DESC", (pid,))
        if r is None:
            raise BizError("RUBRIC_NOT_PUBLISHED", "项目尚无已发布的评价标准，不能标注", status=409)
        return {"id": r["id"], "schema": jloads(r["schema_json"], {})}

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)


def _fingerprint_seed_int(s: str) -> int:
    from .core import sha256_text
    return int(sha256_text(s)[:12], 16)


# ---------------------------------------------------------------- 评价器校准 P07

class JudgeService:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create(self, pid: str, rubric_id: str, model_config: dict) -> dict:
        self._project(pid)
        row = self.db.one("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM judges WHERE project_id=?",
                          (pid,))
        h = canonical_hash({"rubric": rubric_id, "model_config": model_config})
        jid = new_id("jdg")
        self.db.execute(
            "INSERT INTO judges(id,project_id,version_no,rubric_id,model_config_json,status,"
            "config_hash,created_at) VALUES(?,?,?,?,?, 'draft', ?, ?)",
            (jid, pid, row["n"], rubric_id, json.dumps(model_config, ensure_ascii=False), h,
             now_iso()))
        return self.get(jid)

    def get(self, jid: str) -> dict:
        r = self.db.one("SELECT * FROM judges WHERE id=?", (jid,))
        if r is None:
            raise BizError("NOT_FOUND", "评价器不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"], "version_no": r["version_no"],
                "rubric_id": r["rubric_id"], "model_config": jloads(r["model_config_json"], {}),
                "status": r["status"], "build_refs": jloads(r["build_refs_json"], []),
                "audit_refs": jloads(r["audit_refs_json"], []),
                "metrics": jloads(r["metrics_json"], {}), "config_hash": r["config_hash"],
                "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in
                self.db.query("SELECT id FROM judges WHERE project_id=? ORDER BY version_no", (pid,))]

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)

    def calibrate(self, jid: str, build_refs: list[str], audit_refs: list[str],
                  run_id: str = "") -> dict:
        """构建/审计校准。构建集与审计集来源必须隔离（TC021）。

        build_refs / audit_refs 为 output_id 列表，人工 gold 标注必须已核验（TC020）。
        """
        from .engine import evaluate_once
        judge = self.get(jid)
        rubric = self.db.one("SELECT * FROM rubrics WHERE id=?", (judge["rubric_id"],))
        if rubric is None:
            raise BizError("NOT_FOUND", "评价标准不存在", status=404)
        schema = json.loads(rubric["schema_json"])
        dims = [d["name"] for d in schema.get("dimensions", [])]

        def group_of(oid: str) -> str:
            row = self.db.one(
                "SELECT i.source_group_id AS g FROM outputs o JOIN dataset_items i ON i.id=o.item_id"
                " WHERE o.id=?", (oid,))
            return row["g"] if row else f"__output_{oid}"

        overlap = sorted(set(map(group_of, build_refs)) & set(map(group_of, audit_refs)))
        if overlap:
            raise BizError("SOURCE_OVERLAP",
                           f"构建集与审计集存在同来源：{overlap}。审计独立性被破坏，拒绝校准（TC021）",
                           status=422)

        def collect(refs: list[str]) -> list[dict]:
            out = []
            for oid in refs:
                ann = self.db.one(
                    "SELECT * FROM annotations WHERE output_id=? AND gold_status IN"
                    " ('human_verified','adjudicated') ORDER BY created_at DESC", (oid,))
                if ann is None:
                    raise BizError("GOLD_NOT_VERIFIED",
                                   f"输出 {oid} 没有人工核验的gold标注；模型预标注不能作为gold（TC020）")
                out.append({"output_id": oid,
                            "human": jloads(ann["scores_json"], {}).get("scores", {}),
                            "text": self.db.one("SELECT text FROM outputs WHERE id=?",
                                                (oid,))["text"]})
            return out

        build = collect(build_refs)
        audit = collect(audit_refs)
        model_config = judge["model_config"]
        metrics = {"build": self._score_set(build, dims, model_config, rubric["id"], run_id),
                   "audit": self._score_set(audit, dims, model_config, rubric["id"], run_id)}
        low_support = metrics["audit"]["n"] < 5
        metrics["limits"] = {"low_support": low_support,
                             "note": "审计样本不足5条时不能声称全面可靠（TC022）" if low_support else ""}
        self.db.execute("UPDATE judges SET build_refs_json=?, audit_refs_json=?, metrics_json=?,"
                        " status='audited' WHERE id=?",
                        (json.dumps(build_refs), json.dumps(audit_refs),
                         json.dumps(metrics, ensure_ascii=False), jid))
        return self.get(jid)

    def _score_set(self, rows: list[dict], dims: list[str], model_config: dict,
                   rubric_id: str, run_id: str) -> dict:
        from .engine import evaluate_once
        per_dim = {d: {"n": 0, "exact": 0, "within1": 0, "abstain": 0,
                       "confusion": [[0] * 4 for _ in range(4)],
                       "severe_support": 0, "severe_recall": None} for d in dims}
        n = len(rows)
        for row in rows:
            scored = evaluate_once(row["text"], rubric_id, model_config, run_id=run_id)
            if scored.get("abstain"):
                for d in dims:
                    per_dim[d]["abstain"] += 1
                continue
            for d in dims:
                h = row["human"].get(d)
                m = scored["scores"].get(d)
                if isinstance(h, int) and isinstance(m, int):
                    pd = per_dim[d]
                    pd["n"] += 1
                    if h == m:
                        pd["exact"] += 1
                    if abs(h - m) <= 1:
                        pd["within1"] += 1
                    pd["confusion"][min(h, 3)][min(m, 3)] += 1
                    if h == 0:
                        pd["severe_support"] += 1
                        if m == 0:
                            pd["severe_recall"] = (pd["severe_recall"] or 0) + 1
        for d, pd in per_dim.items():
            if pd["n"]:
                pd["exact_rate"] = round(pd["exact"] / pd["n"], 4)
                pd["within1_rate"] = round(pd["within1"] / pd["n"], 4)
                if pd["severe_support"]:
                    pd["severe_recall"] = round((pd["severe_recall"] or 0) / pd["severe_support"], 4)
                else:
                    pd["severe_recall"] = None  # 无正例：召回不可估，不声称高召回（TC022）
        return {"n": n, "per_dim": per_dim}


# ---------------------------------------------------------------- 专家意见与标签（优化1.0 §6.3/§6.4）

SEVERITIES = ("severe", "normal", "preference")       # 严重问题/一般问题/偏好建议
FEEDBACK_STATUSES = ("pending", "confirmed_error", "preference", "unverified",
                     "resolved", "retired")


class FeedbackService:
    """专家意见整理为待确认检查项：原话保留、证据关联、状态与标签可追溯。

    与“学员得分”（AI 对学员作答的业务评分）明确区分：这里是专家对 AI 输出质量的判断。
    """

    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def add(self, pid: str, item_id: str, problem: str, quote: str = "",
            expected: str = "", check_method: str = "", severity: str = "normal",
            status: str = "pending", tags: list[str] | None = None,
            remark: str = "", source: str = "manual") -> dict:
        self._project(pid)
        if not (problem or "").strip():
            raise BizError("FIELD_REQUIRED", "问题描述不能为空", field_errors={"problem": "必填"})
        if severity not in SEVERITIES:
            raise BizError("SEVERITY_INVALID", f"影响程度必须是 {'/'.join(SEVERITIES)}")
        if status not in FEEDBACK_STATUSES:
            raise BizError("STATUS_INVALID", f"状态必须是 {'/'.join(FEEDBACK_STATUSES)}")
        if item_id:
            if self.db.one("SELECT 1 FROM dataset_items WHERE id=? AND project_id=?",
                           (item_id, pid)) is None:
                raise BizError("NOT_FOUND", "案例不存在或不属于该项目", status=404)
        tag_names = self._ensure_tags(pid, tags or [])
        fid = new_id("fbk")
        now = now_iso()
        self.db.execute(
            "INSERT INTO expert_feedback(id,project_id,item_id,quote,problem,expected,"
            "check_method,severity,status,tags_json,remark,source,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, pid, item_id, quote or "", problem.strip(), expected or "", check_method or "",
             severity, status, json.dumps(tag_names, ensure_ascii=False), remark or "",
             source, now, now))
        self._audit(pid, "expert_feedback.add", fid)
        return self.get(fid)

    def update(self, fid: str, changes: dict) -> dict:
        """用户可修改状态/期望/检查方式/影响程度/标签/备注；系统永不改写专家原话与备注原文。"""
        cur = self.get(fid)
        allowed = {"problem", "expected", "check_method", "severity", "status",
                   "remark", "quote", "item_id"}
        sets, params = [], []
        for k, v in changes.items():
            if k not in allowed or v is None:
                continue
            if k == "severity" and v not in SEVERITIES:
                raise BizError("SEVERITY_INVALID", f"影响程度必须是 {'/'.join(SEVERITIES)}")
            if k == "status" and v not in FEEDBACK_STATUSES:
                raise BizError("STATUS_INVALID", f"状态必须是 {'/'.join(FEEDBACK_STATUSES)}")
            if k == "item_id" and v:
                if self.db.one("SELECT 1 FROM dataset_items WHERE id=? AND project_id=?",
                               (v, cur["project_id"])) is None:
                    raise BizError("NOT_FOUND", "案例不存在或不属于该项目", status=404)
            sets.append(f"{k}=?")
            params.append(v)
        if "tags" in changes and changes["tags"] is not None:
            sets.append("tags_json=?")
            params.append(json.dumps(self._ensure_tags(cur["project_id"], changes["tags"]),
                                     ensure_ascii=False))
        if not sets:
            return cur
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(fid)
        self.db.execute(f"UPDATE expert_feedback SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get(fid)

    def get(self, fid: str) -> dict:
        r = self.db.one("SELECT * FROM expert_feedback WHERE id=?", (fid,))
        if r is None:
            raise BizError("NOT_FOUND", "专家意见不存在", status=404)
        return self._row(r)

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "project_id": r["project_id"], "item_id": r["item_id"],
                "quote": r["quote"], "problem": r["problem"], "expected": r["expected"],
                "check_method": r["check_method"], "severity": r["severity"],
                "status": r["status"], "tags": jloads(r["tags_json"], []), "remark": r["remark"],
                "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list(self, pid: str, item_id: str | None = None,
             statuses: list[str] | None = None) -> list[dict]:
        cond, params = ["project_id=?"], [pid]
        if item_id:
            cond.append("item_id=?")
            params.append(item_id)
        if statuses:
            qs = ",".join("?" for _ in statuses)
            cond.append(f"status IN ({qs})")
            params.extend(statuses)
        return [self._row(r) for r in self.db.query(
            f"SELECT * FROM expert_feedback WHERE {' AND '.join(cond)}"
            " ORDER BY created_at", tuple(params))]

    def import_jsonl(self, pid: str, content: str) -> dict:
        """批量导入已有专家评价：逐行报错，不静默丢行；case_id 关联已导入案例。"""
        created, errors = [], []
        for i, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception as e:
                errors.append({"line": i, "reason": f"JSON解析失败：{e}"})
                continue
            case_id = str(row.get("case_id") or "").strip()
            item = self.db.one("SELECT id FROM dataset_items WHERE project_id=? AND case_id=?",
                               (pid, case_id)) if case_id else None
            if case_id and item is None:
                errors.append({"line": i, "case_id": case_id, "reason": "案例不存在：请先导入案例"})
                continue
            if not str(row.get("problem") or "").strip():
                errors.append({"line": i, "case_id": case_id, "reason": "缺少 problem 问题描述"})
                continue
            try:
                created.append(self.add(pid, item["id"] if item else "",
                                        row.get("problem"), quote=row.get("quote", ""),
                                        expected=row.get("expected", ""),
                                        check_method=row.get("check_method", ""),
                                        severity=row.get("severity", "normal"),
                                        status=row.get("status", "pending"),
                                        tags=row.get("tags") or [], remark=row.get("remark", ""),
                                        source="import"))
            except BizError as e:
                errors.append({"line": i, "case_id": case_id, "reason": e.message})
        return {"created": len(created), "errors": errors, "items": created}

    # ---- 标签：创建/定义/停用/同义合并；保留历史与合并关系（§6.4）
    def add_tag(self, pid: str, name: str, definition: str = "") -> dict:
        self._project(pid)
        name = (name or "").strip()
        if not name:
            raise BizError("FIELD_REQUIRED", "标签名不能为空")
        exist = self.db.one("SELECT * FROM tags WHERE project_id=? AND name=?", (pid, name))
        if exist:
            self.db.execute("UPDATE tags SET definition=?, active=1, merged_into='' WHERE id=?",
                            (definition or exist["definition"], exist["id"]))
            return self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (exist["id"],)))
        tid = new_id("tag")
        self.db.execute(
            "INSERT INTO tags(id,project_id,name,definition,active,merged_into,created_at)"
            " VALUES(?,?,?,?,1,'',?)", (tid, pid, name, definition or "", now_iso()))
        return self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (tid,)))

    def list_tags(self, pid: str, include_inactive: bool = True) -> list[dict]:
        cond = "project_id=?" + ("" if include_inactive else " AND active=1 AND merged_into=''")
        return [self._tag(r) for r in self.db.query(
            f"SELECT * FROM tags WHERE {cond} ORDER BY name", (pid,))]

    def merge_tag(self, tag_id: str, into_id: str) -> dict:
        tag = self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (tag_id,)))
        target = self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (into_id,)))
        if tag["project_id"] != target["project_id"]:
            raise BizError("NOT_FOUND", "合并目标不属于同一项目", status=404)
        if tag_id == into_id:
            raise BizError("TAG_MERGE_SELF", "标签不能合并到自身")
        with self.db.tx() as conn:
            conn.execute("UPDATE tags SET merged_into=?, active=0 WHERE id=?", (into_id, tag_id))
            # 历史意见保留原标签并追加目标标签：合并关系可追溯，不重写历史
            for r in conn.execute("SELECT id,tags_json FROM expert_feedback WHERE project_id=?",
                                  (tag["project_id"],)).fetchall():
                names = jloads(r["tags_json"], [])
                if tag["name"] in names:
                    merged = list(dict.fromkeys(names + [target["name"]]))
                    conn.execute("UPDATE expert_feedback SET tags_json=?, updated_at=? WHERE id=?",
                                 (json.dumps(merged, ensure_ascii=False), now_iso(), r["id"]))
        return self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (tag_id,)))

    def retire_tag(self, tag_id: str) -> dict:
        self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (tag_id,)))
        self.db.execute("UPDATE tags SET active=0 WHERE id=?", (tag_id,))
        return self._tag(self.db.one("SELECT * FROM tags WHERE id=?", (tag_id,)))

    def _ensure_tags(self, pid: str, names: list[str]) -> list[str]:
        out = []
        for n in names:
            n = (n or "").strip()
            if not n:
                continue
            row = self.db.one("SELECT id FROM tags WHERE project_id=? AND name=?", (pid, n))
            if row is None:
                self.add_tag(pid, n)
            out.append(n)
        return list(dict.fromkeys(out))

    def suggest_check_aspects(self, pid: str) -> dict:
        """把专家意见归纳为可确认的检查方面（§6.2）：按标签与严重度分组，保留原话入口。"""
        rows = self.list(pid, statuses=["confirmed_error", "pending", "preference"])
        by_tag: dict[str, list[dict]] = {}
        for r in rows:
            for t in (r["tags"] or ["未分类"]):
                by_tag.setdefault(t, []).append(r)
        aspects = [{"tag": t, "count": len(items),
                    "severe": sum(1 for x in items if x["severity"] == "severe"),
                    "samples": [{"id": x["id"], "problem": x["problem"],
                                 "severity": x["severity"], "status": x["status"]}
                                for x in items[:5]]}
                   for t, items in sorted(by_tag.items())]
        return {"aspects": aspects, "open_total": len(rows),
                "note": "以上由系统按标签归纳，需人工确认后作为检查项使用；标签描述现象，不自动等同于原因。"}

    @staticmethod
    def _tag(r) -> dict:
        return {"id": r["id"], "project_id": r["project_id"], "name": r["name"],
                "definition": r["definition"], "active": bool(r["active"]),
                "merged_into": r["merged_into"], "created_at": r["created_at"]}

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)

    def _audit(self, pid: str, action: str, target: str) -> None:
        self.db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (new_id("aud"), "system", action, f"{pid}/{target}", "", now_iso()))


# ---------------------------------------------------------------- 评级规则与案例评级（优化1.0 §6.4/§6.5）

class RatingService:
    """自定义评级（默认ABCD）与逐案例的“点评质量评价”。

    评级规则修改产生新版本，历史评价不被重新解释（§6.4）。
    评级≠学员得分：学员得分是待检查的业务输出，评级是对AI输出质量的专家判断。
    """

    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def create_default(self, pid: str) -> dict:
        """ABCD 首个支持方案（§6.5）：可编辑建议，不是隐藏规则。"""
        self._project(pid)
        return self._insert(pid, {"levels": DEFAULT_ABCD_LEVELS,
                                  "note": "修复原问题但出现严重新增问题时优先判为D；"
                                          "一般问题的取舍由项目定义。始终同时展示原问题改善与新增问题两个维度。"})

    def _insert(self, pid: str, payload: dict) -> dict:
        n = self.db.one("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM rating_rules"
                        " WHERE project_id=?", (pid,))["n"]
        rid = new_id("rul")
        h = canonical_hash(payload)
        self.db.execute(
            "INSERT INTO rating_rules(id,project_id,version_no,levels_json,status,note,hash,"
            "created_at) VALUES(?,?,?,?,'draft',?,?,?)",
            (rid, pid, n, json.dumps(payload, ensure_ascii=False), payload.get("note", ""), h,
             now_iso()))
        return self.get(rid)

    def get(self, rid: str) -> dict:
        r = self.db.one("SELECT * FROM rating_rules WHERE id=?", (rid,))
        if r is None:
            raise BizError("NOT_FOUND", "评级规则不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"], "version_no": r["version_no"],
                "levels": jloads(r["levels_json"], {}).get("levels", []),
                "note": jloads(r["levels_json"], {}).get("note", ""),
                "status": r["status"], "hash": r["hash"], "created_at": r["created_at"]}

    def list(self, pid: str) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query(
            "SELECT id FROM rating_rules WHERE project_id=? ORDER BY version_no", (pid,))]

    def update_draft(self, rid: str, levels: list[dict], note: str = "") -> dict:
        cur = self.get(rid)
        if cur["status"] != "draft":
            raise BizError("RULE_IMMUTABLE", "已发布评级规则不可覆盖；修改产生新版本", status=409)
        self._validate_levels(levels)
        return self._insert(cur["project_id"],
                            {"levels": levels, "note": note or cur["note"]})

    def publish(self, rid: str) -> dict:
        cur = self.get(rid)
        if cur["status"] == "published":
            return cur
        self._validate_levels(cur["levels"])
        self.db.execute("UPDATE rating_rules SET status='published' WHERE id=?", (rid,))
        return self.get(rid)

    @staticmethod
    def _validate_levels(levels: list[dict]) -> None:
        if not levels:
            raise BizError("RULE_INVALID", "评级等级不能为空")
        codes = [str(l.get("code") or "").strip() for l in levels]
        if len(set(codes)) != len(codes) or any(not c for c in codes):
            raise BizError("RULE_INVALID", f"等级代码必须非空且唯一：{codes}")
        for l in levels:
            if l.get("trend") not in ("improved", "partial", "none", "worse", "flat", "unknown"):
                raise BizError("RULE_INVALID",
                               f"等级 {l.get('code')} 的趋势映射必须是 "
                               "improved/partial/none/worse/flat/unknown（不默认把字母转为等距数字求平均）")

    def submit_case_review(self, pid: str, item_id: str, rule_id: str, rating: str,
                           resolutions: list[dict] | None = None,
                           new_problems: list[dict] | None = None,
                           regress_note: str = "", remark: str = "",
                           source: str = "human") -> dict:
        """提交一个案例的点评质量评价；人工与自动来源分别记录，不互相静默覆盖（§6.6）。"""
        self._project(pid)
        rule = self.get(rule_id)
        if rule["project_id"] != pid:
            raise BizError("NOT_FOUND", "评级规则不属于该项目", status=404)
        if self.db.one("SELECT 1 FROM dataset_items WHERE id=? AND project_id=?",
                       (item_id, pid)) is None:
            raise BizError("NOT_FOUND", "案例不存在或不属于该项目", status=404)
        valid_codes = {l["code"] for l in rule["levels"]} | set(RATING_SPECIAL)
        if rating not in valid_codes:
            raise BizError("RATING_INVALID", f"评级必须是 {sorted(valid_codes)} 之一")
        if source not in ("human", "auto_suggested", "human_confirmed", "human_corrected"):
            raise BizError("SOURCE_INVALID", "评价来源标记非法")
        for r in (resolutions or []):
            if r.get("status") not in RESOLUTION_STATES:
                raise BizError("RESOLUTION_INVALID",
                               f"问题解决状态必须是 {'/'.join(RESOLUTION_STATES)}")
        for p in (new_problems or []):
            if not str(p.get("description") or "").strip():
                raise BizError("FIELD_REQUIRED", "新增问题必须给出描述")
        cid = new_id("crv")
        self.db.execute(
            "INSERT INTO case_reviews(id,project_id,item_id,rule_id,rating,resolutions_json,"
            "new_problems_json,regress_note,remark,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cid, pid, item_id, rule_id, rating,
             json.dumps(resolutions or [], ensure_ascii=False),
             json.dumps(new_problems or [], ensure_ascii=False),
             regress_note or "", remark or "", source, now_iso()))
        return self.get_review(cid)

    def get_review(self, cid: str) -> dict:
        r = self.db.one("SELECT * FROM case_reviews WHERE id=?", (cid,))
        if r is None:
            raise BizError("NOT_FOUND", "案例评级不存在", status=404)
        return {"id": r["id"], "project_id": r["project_id"], "item_id": r["item_id"],
                "rule_id": r["rule_id"], "rating": r["rating"],
                "resolutions": jloads(r["resolutions_json"], []),
                "new_problems": jloads(r["new_problems_json"], []),
                "regress_note": r["regress_note"], "remark": r["remark"], "source": r["source"],
                "created_at": r["created_at"]}

    def list_reviews(self, pid: str, item_id: str | None = None) -> list[dict]:
        cond, params = "project_id=?", [pid]
        if item_id:
            cond += " AND item_id=?"
            params.append(item_id)
        return [self.get_review(r["id"]) for r in self.db.query(
            f"SELECT * FROM case_reviews WHERE {cond} ORDER BY created_at", tuple(params))]

    def suggest_review(self, pid: str, item_id: str, rule_id: str,
                       problem_statuses: dict[str, str] | None = None,
                       new_problems: list[dict] | None = None) -> dict:
        """按 §6.5 默认规则给出评级建议：严重新增问题优先D；无法判断单独记录。

        problem_statuses: {feedback_id: fixed/partial/open/unknown}
        """
        rule = self.get(rule_id)
        statuses = dict(problem_statuses or {})
        fbs = {f["id"]: f for f in FeedbackService(self.db).list(pid, item_id=item_id)}
        resolutions = []
        for fid, fb in fbs.items():
            if fid in statuses:
                resolutions.append({"feedback_id": fid, "status": statuses[fid],
                                    "problem": fb["problem"], "severity": fb["severity"]})
            elif fb["status"] == "confirmed_error":
                # 已确认但未给出核查结果：记无法判断，不默认解决（§6.5）
                resolutions.append({"feedback_id": fid, "status": "unknown",
                                    "problem": fb["problem"], "severity": fb["severity"]})
        st = [r["status"] for r in resolutions]
        severe_new = any((p.get("severity") == "severe") for p in (new_problems or []))
        if severe_new:
            rating = "D"
        elif not st:
            rating = "not_rated"
        elif "unknown" in st:
            rating = "cannot_judge"
        elif all(s == "fixed" for s in st):
            rating = "A"
        elif all(s in ("open",) for s in st):
            rating = "C"
        else:
            rating = "B"
        basis = {"A": "全部原问题解决；新增问题需同时查看",
                 "B": "部分原问题已解决",
                 "C": "原问题仍未解决",
                 "D": "出现严重新增问题，优先判D（§6.5）",
                 "cannot_judge": "存在无法判断的问题项：单独记录，不并入等级",
                 "not_rated": "该案例没有已登记的专家问题"}[rating]
        return {"item_id": item_id, "rule_id": rule_id, "rating": rating,
                "resolutions": resolutions, "new_problems": new_problems or [],
                "basis": basis, "source": "auto_suggested",
                "note": "建议需人工确认后生效（§6.5：不能只用一个字母，两个维度同时展示）"}

    def _project(self, pid: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)) is None:
            raise BizError("NOT_FOUND", "项目不存在", status=404)


def jloads2(db, row, key, default):
    return jloads(row[key], default) if row is not None else default

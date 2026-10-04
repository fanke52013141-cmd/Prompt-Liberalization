"""FastAPI 路由层：/workflow-api/v1 前缀 + 静态界面。

默认只绑定 127.0.0.1（本机使用，TC058）；密钥从不出现在响应中（TC051）。
错误体统一 code/message/field_errors/retryable/correlation_id。
"""
import json
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import BizError
from .db import get_db
from .domain import (AnnotationService, DataService, FeedbackService, JudgeService,
                     ProjectsService, PromptService, RatingService, RubricService)
from .runs import AcceptanceService, ReleaseService, RunService

API = "/workflow-api/v1"
WEB_DIR = None  # 在 create_app 时设置


def _cid() -> str:
    return uuid.uuid4().hex[:12]


def create_app(web_dir: str) -> FastAPI:
    global WEB_DIR
    WEB_DIR = web_dir
    app = FastAPI(title="提示词优化实验室", version="0.1.0", docs_url=None, redoc_url=None)
    _seed()

    @app.exception_handler(BizError)
    async def biz_handler(request: Request, exc: BizError):
        return JSONResponse(status_code=exc.status, content=exc.body(_cid()))

    @app.exception_handler(Exception)
    async def any_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={
            "code": "INTERNAL", "message": f"内部错误：{exc}", "field_errors": {},
            "retryable": False, "correlation_id": _cid()})

    reg = _routes(app)

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        """界面静态资源走协商缓存：本地更新后刷新页面即可用到新版本，不会被旧缓存卡住。"""
        response = await call_next(request)
        if request.url.path in ("/", "/index.html", "/app.js", "/style.css"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def _seed() -> None:
    """首次启动写入默认离线模拟连接。"""
    db = get_db()
    if db.one("SELECT 1 FROM settings WHERE key='connections'") is None:
        conns = [{"id": "conn_mock", "name": "内置离线模拟供应商", "provider": "mock",
                  "model": "mock-gen-1", "base_url": "", "api_key": "",
                  "unsupported_params": []}]
        db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES('connections',?,?)",
                   (json.dumps(conns, ensure_ascii=False), ""))
    if db.one("SELECT 1 FROM settings WHERE key='prices'") is None:
        db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES('prices',?,'')",
                   (json.dumps({}, ensure_ascii=False),))


def _routes(app: FastAPI):
    @app.get(API + "/templates")
    def templates():
        from .core import TASK_TEMPLATES, template_draft
        return {k: template_draft(k) for k in TASK_TEMPLATES}

    # ---------------- 项目 P01/P02
    class ProjectIn(BaseModel):
        name: str
        description: str = ""
        task_type: str = "custom"
        contract: dict | None = None   # 自定义任务契约（模板仅是可选示例，优化1.0 §3.2）
        goal: str = ""                 # 优化目标：自由描述

    @app.post(API + "/projects", status_code=201)
    def create_project(body: ProjectIn):
        return ProjectsService().create(body.name, body.description, body.task_type,
                                        body.contract, body.goal)

    @app.get(API + "/projects")
    def list_projects():
        return {"projects": ProjectsService().list()}

    @app.get(API + "/projects/{pid}")
    def get_project(pid: str):
        return ProjectsService().get(pid)

    class GoalIn(BaseModel):
        goal: str

    @app.put(API + "/projects/{pid}/goal")
    def update_goal(pid: str, body: GoalIn):
        return ProjectsService().update_goal(pid, body.goal)

    @app.get(API + "/projects/{pid}/progress")
    def progress(pid: str):
        return ProjectsService().progress(pid)

    @app.delete(API + "/projects/{pid}")
    def delete_project(pid: str):
        """硬删除项目及全部从属数据（测试阶段能力，不可恢复）。"""
        return ProjectsService().delete(pid)

    @app.post(API + "/projects/{pid}/archive")
    def archive(pid: str):
        return ProjectsService().archive(pid, True)

    @app.post(API + "/projects/{pid}/unarchive")
    def unarchive(pid: str):
        return ProjectsService().archive(pid, False)

    @app.post(API + "/demo/seed")
    def demo_seed():
        """生成（或复用）内置示例项目：离线模拟数据完整跑通一次优化流程，供用户浏览学习。"""
        from .demo_seed import seed_demo
        return seed_demo()

    @app.get(API + "/projects/{pid}/readiness")
    def readiness(pid: str):
        return ProjectsService().readiness(pid)

    @app.get(API + "/projects/{pid}/outbound-preview")
    def outbound_preview(pid: str):
        """数据外发范围预览（07 方案 R15/TC078）：各角色收到的字段一目了然。"""
        contract = ProjectsService().get(pid)["contract"]
        runtime = [f.get("label") or f.get("name") for f in contract.get("runtime_fields", [])]
        evalf = [f.get("label") or f.get("name") for f in contract.get("evaluation_fields", [])]
        return {
            "roles": [
                {"role": "生成（执行 AI）", "receives": runtime,
                 "note": "只收到输入字段的案例内容"},
                {"role": "评价器", "receives": ["模型输出正文", "评价标准维度"] + evalf,
                 "note": "评价专用字段（如参考答案）只给评价器，用于判断输出质量"},
                {"role": "优化器（反思）",
                 "receives": ["失败案例的输入与输出摘录", "已验证问题与简短理由"],
                 "note": "只使用开发/选择侧证据；接触不到封存考题"},
            ],
            "never_sent": ["评价专用字段绝不进入生成请求（防止抄答案）",
                           "封存考题原文在解封前不出站",
                           "API 密钥不出现在任何请求与日志"],
        }

    @app.get(API + "/reports/{rep_id}/package")
    def report_package(rep_id: str):
        """导出使用包（07 方案 R13/§19.1/TC076）：文本+manifest，不含密钥与封存原文。"""
        rep = AcceptanceService().get(rep_id)
        cand = PromptService().get(rep["candidate_ref"])
        base = PromptService().get(rep["baseline_ref"])
        proj = ProjectsService().get(rep["project_id"])
        rel = ReleaseService()
        cur = rel.current(rep["project_id"])
        if cur and cur["prompt_version_id"] == cand["id"]:
            status = "正式采用（active）"
        else:
            trials = [h for h in rel.history(rep["project_id"])
                      if h["prompt_version_id"] == cand["id"] and h["status"] == "trial"]
            status = "试用（未验证指针）" if trials else "未采用（仅验证记录）"
        return {
            "package_version": "1.0",
            "generated_at": rep["created_at"],
            "task": {"name": proj["name"], "type": proj["task_type"],
                     "evaluation_unit": proj["contract"].get("evaluation_unit"),
                     "runtime_fields": proj["contract"].get("runtime_fields")},
            "prompt": {"name": cand["name"], "version_no": cand["version_no"],
                       "body": cand["body"], "variables": cand["variables"],
                       "hash": cand["hash"]},
            "baseline": {"name": base["name"], "version_no": base["version_no"],
                         "hash": base["hash"]},
            "verification": {"report_id": rep["id"], "decision": rep["decision"],
                             "eligibility": rep.get("eligibility", ""),
                             "diff": rep["stats"].get("diff"),
                             "group_n": rep["stats"].get("group_n"),
                             "scope_note": "验证仅针对已记录配置与考题来源范围；"
                                           "更换模型或删改提示词后应重新比较"},
            "adoption_status": status,
            "note": "本使用包不含密钥、封存原文或评价专用资料",
        }

    # ---------------- 数据 P03
    class ImportIn(BaseModel):
        source_name: str = "粘贴导入"
        fmt: str
        content: str
        field_map: dict | None = None

    @app.post(API + "/projects/{pid}/imports/preview")
    def import_preview(pid: str, body: ImportIn):
        batch = DataService().preview_import(pid, body.source_name, body.fmt, body.content,
                                             body.field_map)
        # 保存有效行到受限区供提交复核
        items = _batch_valid_items(pid, body.fmt, body.content, batch["preview_hash"])
        DataService().store_source(batch["id"], batch["preview_hash"], items)
        return batch

    def _batch_valid_items(pid, fmt, content, preview_hash):
        """重放预览逻辑取有效行（与 preview_import 相同校验）。"""
        import io, csv
        contract = ProjectsService().get(pid)["contract"]
        runtime_names = [f["name"] for f in contract.get("runtime_fields", [])]
        required = [f["name"] for f in contract.get("runtime_fields", []) if f.get("required", True)]
        rows, valid, seen = [], [], set()
        if fmt == "jsonl":
            for line in content.splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        elif fmt == "csv":
            for raw in csv.DictReader(io.StringIO(content)):
                row = {}
                for col, val in raw.items():
                    if col is None:
                        continue
                    if col in ("case_id", "source_group_id", "origin", "scene_tags"):
                        row[col] = val
                    elif col.startswith("runtime:"):
                        row.setdefault("runtime_input", {})[col.split(":", 1)[1]] = val
                    elif col.startswith("eval:"):
                        row.setdefault("evaluation_only", {})[col.split(":", 1)[1]] = val
                    else:
                        row.setdefault("runtime_input", {})[col] = val
                rows.append(row)
        for row in rows:
            case_id = str(row.get("case_id") or "").strip()
            ri = row.get("runtime_input") or {}
            if not case_id or case_id in seen:
                continue
            if any(not str(ri.get(f) or "").strip() for f in required):
                continue
            if any(f not in runtime_names for f in ri):
                continue
            seen.add(case_id)
            valid.append({"case_id": case_id,
                          "source_group_id": str(row.get("source_group_id") or case_id),
                          "origin": row.get("origin") or "real",
                          "scene_tags": row.get("scene_tags") or "",
                          "runtime_input": ri,
                          "evaluation_only": row.get("evaluation_only") or {}})
        return valid

    class CommitIn(BaseModel):
        exclude_case_ids: list[str] = []

    @app.post(API + "/projects/{pid}/imports/{batch_id}/commit")
    def import_commit(pid: str, batch_id: str, body: CommitIn):
        return DataService().commit_import(pid, batch_id, body.exclude_case_ids)

    @app.get(API + "/projects/{pid}/items")
    def items(pid: str, split: str | None = None, page: int = 1, size: int = 50):
        return DataService().list_items(pid, split, page, size)

    class GroupsIn(BaseModel):
        assignments: dict

    @app.post(API + "/projects/{pid}/groups")
    def set_groups(pid: str, body: GroupsIn):
        return DataService().set_groups(pid, body.assignments)

    class SplitIn(BaseModel):
        case_ids: list[str]
        split: str

    @app.post(API + "/projects/{pid}/split")
    def assign_split(pid: str, body: SplitIn):
        return DataService().assign_split(pid, body.case_ids, body.split)

    class FreezeIn(BaseModel):
        seed: int = 20260927
        weights: dict = {}

    @app.post(API + "/projects/{pid}/manifests/freeze")
    def freeze(pid: str, body: FreezeIn):
        return DataService().freeze_manifest(pid, body.seed, body.weights)

    @app.get(API + "/projects/{pid}/manifests")
    def manifests(pid: str):
        rows = get_db().query("SELECT * FROM split_manifests WHERE project_id=?", (pid,))
        sealed_n = get_db().one(
            "SELECT COUNT(*) AS c FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'",
            (pid,))["c"]
        return {"manifests": [{"id": r["id"], "dataset_version_id": r["dataset_version_id"],
                               "group_map_hash": r["group_map_hash"], "seed": r["seed"],
                               "state": r["state"], "created_at": r["created_at"]} for r in rows],
                "sealed_count": sealed_n}

    @app.get(API + "/projects/{pid}/dataset_versions")
    def dsvs(pid: str):
        rows = get_db().query(
            "SELECT id,version_no,note,hash,created_at FROM dataset_versions WHERE project_id=?"
            " ORDER BY version_no", (pid,))
        return {"versions": [dict(r) for r in rows]}

    # ---------------- 评价标准 P04
    @app.post(API + "/projects/{pid}/rubrics", status_code=201)
    def rubric_draft(pid: str):
        return RubricService().create_draft(pid)

    @app.get(API + "/projects/{pid}/rubrics")
    def rubrics_list(pid: str):
        return {"rubrics": RubricService().list(pid)}

    @app.get(API + "/rubrics/{rid}")
    def rubric_get(rid: str):
        return RubricService().get(rid)

    @app.put(API + "/rubrics/{rid}")
    def rubric_update(rid: str, body: dict):
        return RubricService().update_draft(rid, body)

    @app.post(API + "/rubrics/{rid}/publish")
    def rubric_publish(rid: str):
        return RubricService().publish(rid)

    # ---------------- 提示词 P08/P09
    class PromptIn(BaseModel):
        name: str
        body: str
        frozen_segments: list[dict] = []
        variables: list[str] = []
        params: dict = {}
        parent_id: str = ""
        origin: str = "manual"
        hypothesis: str = ""

    @app.post(API + "/projects/{pid}/prompts", status_code=201)
    def prompt_create(pid: str, body: PromptIn):
        return PromptService().create_version(pid, body.name, body.body, body.frozen_segments,
                                              body.variables, body.params, body.parent_id,
                                              body.origin, body.hypothesis)

    @app.get(API + "/projects/{pid}/prompts")
    def prompt_list(pid: str):
        return {"prompts": PromptService().list(pid)}

    @app.get(API + "/prompts/{pvid}")
    def prompt_get(pvid: str):
        return PromptService().get(pvid)

    @app.get(API + "/prompts/{a_id}/diff")
    def prompt_diff(a_id: str, b_id: str):
        return PromptService().diff(a_id, b_id)

    class TrialIn(BaseModel):
        item_id: str
        generation_model: dict = {}
        evaluation: bool = True

    @app.post(API + "/prompts/{pvid}/trial")
    def prompt_trial(pvid: str, body: TrialIn):
        """试运行：单条真实请求；所有调用经计量入口（BR09）。"""
        from .ledger import BudgetState, Ledger
        from .engine import generate_once, evaluate_once
        pv = PromptService().get(pvid)
        project = ProjectsService().get(pv["project_id"])
        item = DataService().get_item_runtime(project["id"], body.item_id)
        model_config = body.generation_model or {"connection_id": "conn_mock", "model": "mock-gen-1"}
        budget = BudgetState({"mode": "token", "total_limit": 10 ** 9, "search_limit": 10 ** 9,
                              "acceptance_limit": 10 ** 9})
        run_id = f"trial_{pvid}"
        gen = generate_once(get_db(), project, pv, item, model_config, run_id, "search",
                            budget, Ledger(get_db()))
        out = {"generation": gen}
        if body.evaluation and gen["status"] == "ok":
            rubric = get_db().one("SELECT id FROM rubrics WHERE project_id=? AND status='published'"
                                  " ORDER BY version_no DESC", (project["id"],))
            if rubric:
                text = get_db().one("SELECT text FROM outputs WHERE id=?", (gen["id"],))["text"]
                out["evaluation"] = evaluate_once(text, rubric["id"], model_config, run_id)
        return out

    # ---------------- 输出与标注 P05/P06
    @app.get(API + "/outputs/{oid}")
    def output_get(oid: str):
        r = get_db().one("SELECT * FROM outputs WHERE id=?", (oid,))
        if r is None:
            raise BizError("NOT_FOUND", "输出不存在", status=404)
        return {"id": r["id"], "item_id": r["item_id"], "prompt_version_id": r["prompt_version_id"],
                "run_id": r["run_id"], "text": r["text"], "status": r["status"],
                "usage": json.loads(r["usage_json"] or "{}")}

    @app.get(API + "/projects/{pid}/outputs")
    def outputs_list(pid: str, run_id: str | None = None, limit: int = 100):
        cond, params = "project_id=?", [pid]
        if run_id:
            cond += " AND run_id=?"
            params.append(run_id)
        rows = get_db().query(f"SELECT * FROM outputs WHERE {cond} ORDER BY created_at DESC LIMIT ?",
                              tuple(params + [limit]))
        return {"outputs": [{"id": r["id"], "item_id": r["item_id"],
                             "prompt_version_id": r["prompt_version_id"], "run_id": r["run_id"],
                             "status": r["status"]} for r in rows]}

    class PairIn(BaseModel):
        left_output_id: str
        right_output_id: str
        purpose: str = "blind_ab"

    @app.post(API + "/projects/{pid}/pairs", status_code=201)
    def pair_create(pid: str, body: PairIn):
        return AnnotationService().create_pair(pid, body.left_output_id, body.right_output_id,
                                               body.purpose)

    @app.get(API + "/pairs/{public_id}/blind")
    def pair_blind(public_id: str):
        return AnnotationService().get_pair_blind(public_id)

    @app.post(API + "/pairs/{public_id}/reveal")
    def pair_reveal(public_id: str):
        return AnnotationService().reveal(public_id)

    @app.post(API + "/pairs/{public_id}/annotations", status_code=201)
    def annotate(public_id: str, payload: dict):
        return AnnotationService().submit_annotation(public_id, payload)

    @app.get(API + "/projects/{pid}/annotations")
    def annotations(pid: str, gold_only: bool = False):
        return {"annotations": AnnotationService().list_annotations(pid, gold_only)}

    @app.post(API + "/annotations/{aid}/verify")
    def verify_annotation(aid: str):
        return AnnotationService().verify_annotation(aid)

    # ---------------- 评价器 P07
    class JudgeIn(BaseModel):
        rubric_id: str
        model_cfg: dict

    @app.post(API + "/projects/{pid}/judges", status_code=201)
    def judge_create(pid: str, body: JudgeIn):
        return JudgeService().create(pid, body.rubric_id, body.model_cfg)

    @app.get(API + "/projects/{pid}/judges")
    def judges_list(pid: str):
        return {"judges": JudgeService().list(pid)}

    @app.get(API + "/judges/{jid}")
    def judge_get(jid: str):
        return JudgeService().get(jid)

    class CalibrateIn(BaseModel):
        build_refs: list[str]
        audit_refs: list[str]

    @app.post(API + "/judges/{jid}/calibrate")
    def judge_calibrate(jid: str, body: CalibrateIn):
        return JudgeService().calibrate(jid, body.build_refs, body.audit_refs)

    # ---------------- 运行 P10/P11
    @app.post(API + "/projects/{pid}/runs/validate")
    def run_validate(pid: str, draft: dict):
        snap = RunService().validate_snapshot(pid, draft)
        return {"valid": True, "snapshot": snap, "estimate": RunService().estimate(draft, pid)}

    @app.post(API + "/projects/{pid}/runs", status_code=202)
    def run_create(pid: str, draft: dict, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        snap = RunService().validate_snapshot(pid, draft)
        run = RunService().create_run(pid, snap, key)
        if run["state"] == "queued":
            RunService().start(run["id"])
        return run

    @app.get(API + "/runs/{rid}")
    def run_get(rid: str, detail: bool = False):
        return RunService().get(rid, detail=detail)

    @app.post(API + "/runs/{rid}/continue")
    def run_continue(rid: str):
        return RunService().continue_run(rid)

    @app.get(API + "/runs/{rid}/events")
    def run_events(rid: str, cursor: int = 0):
        return {"events": RunService().events(rid, cursor)}

    @app.post(API + "/runs/{rid}/cancel")
    def run_cancel(rid: str, body: dict):
        return RunService().cancel(rid, int(body.get("revision", 0)))

    @app.post(API + "/runs/{rid}/resume")
    def run_resume(rid: str):
        RunService().resume(rid)
        return RunService().get(rid)

    class LockIn(BaseModel):
        candidate_id: str

    @app.post(API + "/runs/{rid}/lock")
    def run_lock(rid: str, body: LockIn):
        return RunService().lock_candidate(rid, body.candidate_id)

    @app.get(API + "/runs/{rid}/ledger")
    def run_ledger(rid: str):
        return RunService().ledger_view(rid)

    @app.get(API + "/projects/{pid}/runs")
    def runs_list(pid: str):
        return {"runs": RunService().list(pid)}

    # ---------------- 验收与报告 P12
    @app.post(API + "/runs/{rid}/accept", status_code=202)
    def run_accept(rid: str):
        return AcceptanceService().accept(rid)

    @app.get(API + "/reports/{rep_id}")
    def report_get(rep_id: str):
        return AcceptanceService().get(rep_id)

    @app.get(API + "/projects/{pid}/reports")
    def reports_list(pid: str):
        return {"reports": AcceptanceService().list(pid)}

    # ---------------- 发布与反馈 P13
    class ReleaseIn(BaseModel):
        prompt_version_id: str
        report_ref: str = ""
        mode: str = "active"
        expected_revision: int | None = None
        model_cfg: dict = {}

    @app.post(API + "/projects/{pid}/releases", status_code=201)
    def release_create(pid: str, body: ReleaseIn):
        return ReleaseService().adopt(pid, body.prompt_version_id, body.report_ref, body.mode,
                                      body.expected_revision, body.model_cfg or None)

    @app.get(API + "/projects/{pid}/releases")
    def release_list(pid: str):
        return {"current": ReleaseService().current(pid), "history": ReleaseService().history(pid)}

    @app.post(API + "/releases/{rid}/rollback")
    def release_rollback(rid: str, body: dict):
        return ReleaseService().rollback(rid, body["target_release_id"])

    @app.post(API + "/releases/{rid}/feedback", status_code=201)
    def release_feedback(rid: str, body: dict):
        return ReleaseService().submit_feedback(rid, body.get("adoption", ""),
                                                body.get("edit_time", ""),
                                                body.get("reason", ""))

    @app.get(API + "/projects/{pid}/feedback")
    def feedback_list(pid: str):
        return {"feedback": ReleaseService().feedback_list(pid)}

    # ---------------- 专家意见与标签（优化1.0 §6.3/§6.4）
    class FeedbackIn(BaseModel):
        item_id: str = ""
        problem: str
        quote: str = ""
        expected: str = ""
        check_method: str = ""
        severity: str = "normal"
        status: str = "pending"
        tags: list[str] = []
        remark: str = ""

    @app.post(API + "/projects/{pid}/expert_feedback", status_code=201)
    def feedback_add(pid: str, body: FeedbackIn):
        return FeedbackService().add(pid, body.item_id, body.problem, body.quote,
                                     body.expected, body.check_method, body.severity,
                                     body.status, body.tags, body.remark)

    @app.get(API + "/projects/{pid}/expert_feedback")
    def feedback_list(pid: str, item_id: str | None = None):
        return {"feedback": FeedbackService().list(pid, item_id=item_id)}

    @app.put(API + "/expert_feedback/{fid}")
    def feedback_update(fid: str, body: dict):
        return FeedbackService().update(fid, body)

    class FeedbackImportIn(BaseModel):
        content: str

    @app.post(API + "/projects/{pid}/expert_feedback/import")
    def feedback_import(pid: str, body: FeedbackImportIn):
        return FeedbackService().import_jsonl(pid, body.content)

    @app.get(API + "/projects/{pid}/expert_feedback/suggestions")
    def feedback_suggestions(pid: str):
        return FeedbackService().suggest_check_aspects(pid)

    class TagIn(BaseModel):
        name: str
        definition: str = ""

    @app.post(API + "/projects/{pid}/tags", status_code=201)
    def tag_add(pid: str, body: TagIn):
        return FeedbackService().add_tag(pid, body.name, body.definition)

    @app.get(API + "/projects/{pid}/tags")
    def tag_list(pid: str):
        return {"tags": FeedbackService().list_tags(pid)}

    class TagMergeIn(BaseModel):
        into_id: str

    @app.post(API + "/tags/{tag_id}/merge")
    def tag_merge(tag_id: str, body: TagMergeIn):
        return FeedbackService().merge_tag(tag_id, body.into_id)

    @app.post(API + "/tags/{tag_id}/retire")
    def tag_retire(tag_id: str):
        return FeedbackService().retire_tag(tag_id)

    # ---------------- 评级规则与案例评级（优化1.0 §6.4/§6.5）
    @app.post(API + "/projects/{pid}/rating_rules", status_code=201)
    def rating_default(pid: str):
        return RatingService().create_default(pid)

    @app.get(API + "/projects/{pid}/rating_rules")
    def rating_list(pid: str):
        return {"rules": RatingService().list(pid)}

    @app.get(API + "/rating_rules/{rid}")
    def rating_get(rid: str):
        return RatingService().get(rid)

    class RatingUpdateIn(BaseModel):
        levels: list[dict]
        note: str = ""

    @app.put(API + "/rating_rules/{rid}")
    def rating_update(rid: str, body: RatingUpdateIn):
        return RatingService().update_draft(rid, body.levels, body.note)

    @app.post(API + "/rating_rules/{rid}/publish")
    def rating_publish(rid: str):
        return RatingService().publish(rid)

    class ReviewIn(BaseModel):
        item_id: str
        rule_id: str
        rating: str
        resolutions: list[dict] = []
        new_problems: list[dict] = []
        regress_note: str = ""
        remark: str = ""
        source: str = "human"

    @app.post(API + "/projects/{pid}/case_reviews", status_code=201)
    def review_submit(pid: str, body: ReviewIn):
        return RatingService().submit_case_review(
            pid, body.item_id, body.rule_id, body.rating, body.resolutions,
            body.new_problems, body.regress_note, body.remark, body.source)

    @app.get(API + "/projects/{pid}/case_reviews")
    def review_list(pid: str, item_id: str | None = None):
        return {"reviews": RatingService().list_reviews(pid, item_id=item_id)}

    class ReviewSuggestIn(BaseModel):
        item_id: str
        rule_id: str
        problem_statuses: dict = {}
        new_problems: list[dict] = []

    @app.post(API + "/projects/{pid}/case_reviews/suggest")
    def review_suggest(pid: str, body: ReviewSuggestIn):
        return RatingService().suggest_review(pid, body.item_id, body.rule_id,
                                              body.problem_statuses, body.new_problems)

    # ---------------- 设置 P14
    @app.get(API + "/settings/connections")
    def conns_get():
        row = get_db().one("SELECT value_json FROM settings WHERE key='connections'")
        conns = json.loads(row["value_json"]) if row else []
        masked = [{**c, "api_key": ("已配置" if c.get("api_key") else "")} for c in conns]
        return {"connections": masked}  # 密钥永不返回（TC051）

    class ConnIn(BaseModel):
        id: str = ""
        name: str
        provider: str
        model: str = ""
        base_url: str = ""
        api_key: str = ""
        unsupported_params: list[str] = []

    @app.put(API + "/settings/connections")
    def conns_put(body: ConnIn):
        db = get_db()
        row = db.one("SELECT value_json FROM settings WHERE key='connections'")
        conns = json.loads(row["value_json"]) if row else []
        entry = body.model_dump()
        if not entry["id"]:
            entry["id"] = "conn_" + uuid.uuid4().hex[:8]
        else:
            # 不传密钥时保留原密钥
            old = next((c for c in conns if c["id"] == entry["id"]), None)
            if old and not entry["api_key"]:
                entry["api_key"] = old.get("api_key", "")
        conns = [c for c in conns if c["id"] != entry["id"]]
        conns.append(entry)
        db.execute("UPDATE settings SET value_json=? WHERE key='connections'",
                   (json.dumps(conns, ensure_ascii=False),))
        return {"saved": True, "id": entry["id"]}

    @app.post(API + "/settings/connections/{cid}/test")
    def conn_test(cid: str):
        from .engine import get_connection, call_model
        from .ledger import BudgetState, Ledger
        mc = {"connection_id": cid}
        try:
            r = call_model(get_db(), "generation", mc,
                           [{"role": "user", "content": "连接测试：请回复OK"}], {},
                           "conn_test", "lrq_test", "search", BudgetState(
                               {"mode": "token", "total_limit": 10 ** 9, "search_limit": 10 ** 9,
                                "acceptance_limit": 10 ** 9}), Ledger(get_db()))
            return {"ok": True, "sample": r.text[:50], "usage": r.usage}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get(API + "/settings/prices")
    def prices_get():
        row = get_db().one("SELECT value_json FROM settings WHERE key='prices'")
        return {"prices": json.loads(row["value_json"]) if row else {}}

    @app.put(API + "/settings/prices")
    def prices_put(prices: dict):
        get_db().execute("UPDATE settings SET value_json=? WHERE key='prices'",
                         (json.dumps(prices, ensure_ascii=False),))
        return {"saved": True}

    @app.get(API + "/settings/diagnostics")
    def diagnostics():
        """脱敏诊断导出：不含密钥与封存原文（TC051/TC060）。"""
        db = get_db()
        return {
            "version": "0.1.0",
            "counts": {t: db.one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in
                       ("projects", "dataset_items", "outputs", "runs", "ledger",
                        "acceptance_reports", "releases")},
            "audit_recent": [dict(r) for r in db.query(
                "SELECT action,target,created_at FROM audit_log ORDER BY created_at DESC LIMIT 20")],
        }

    @app.get(API + "/audit")
    def audit():
        rows = get_db().query("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 100")
        return {"audit": [dict(r) for r in rows]}


# run_server.py 使用：
#   uvicorn.prompt_lab_app:app 由 create_app 生成，见仓库根 run_server.py

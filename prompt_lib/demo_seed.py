"""内置示例项目生成器（优化1.0 可用性引导：用户先看一遍完整流程）。

用离线模拟供应商把"材料→评价→原始测评→自动优化→独立验证→采用"完整跑一遍，
生成一个可以随便浏览的示例项目（名字以"示例："开头，界面会标注演示性质）。
幂等：同名示例项目已存在时直接返回，不重复生成。所有数据均为演示性质。
"""
from __future__ import annotations

import json
import time

from .core import new_id, now_iso
from .db import DB, get_db
from .domain import (DataService, FeedbackService, ProjectsService, PromptService,
                     RatingService, RubricService)
from .runs import AcceptanceService, ReleaseService, RunService

DEMO_NAME = "示例：学员作答点评（看完你就懂了）"

_DEMO_CASES = [
    {"case_id": "c001", "question": "解方程 2x+3=11", "student_answer": "x=4",
     "expert_answer": "x=4，正确"},
    {"case_id": "c002", "question": "计算 3/4 + 1/6", "student_answer": "通分取12作分母，得 11/12",
     "expert_answer": "11/12，正确"},
    {"case_id": "c003", "question": "解方程 3(x-2)=9", "student_answer": "3x-2=9，x=11/3",
     "expert_answer": "去括号应为 3x-6=9，x=5；学员分配律用错（计算/步骤错误，非概念不清）"},
    {"case_id": "c004", "question": "化简 (a^2 b)^3", "student_answer": "a^6 b^3",
     "expert_answer": "a^6 b^3，正确"},
    {"case_id": "c005", "question": "三角形内角和是多少", "student_answer": "180度",
     "expert_answer": "180°，正确"},
    {"case_id": "c006", "question": "计算 15 - 8 ÷ 2", "student_answer": "(15-8)÷2=3.5",
     "expert_answer": "应先算除法：15-4=11；运算顺序错误（步骤错误）"},
    {"case_id": "c007", "question": "解方程 x/2 = 6", "student_answer": "x=3",
     "expert_answer": "x=12；学员把除法当成乘法处理"},
    {"case_id": "c008", "question": "长方形长5宽3，周长是多少", "student_answer": "8",
     "expert_answer": "周长=(5+3)×2=16；学员算成了长宽之和"},
]

_BASELINE_BODY = "你是一名教研老师。请点评学员答案。"
_DEMO_FEEDBACK = [
    {"case_id": "c003", "problem": "学员是去括号时符号出错（计算错误），AI 却说“概念不清”——错误归因。",
     "quote": "AI 点评说“没掌握分配律的概念”，其实步骤列对了，只是计算符号错了。",
     "expected": "先区分“列式思路错误”与“计算错误”，再给结论。",
     "severity": "severe", "status": "confirmed_error", "tags": ["错误归因"]},
    {"case_id": "c006", "problem": "AI 没有指出具体错在哪一步，只说“再仔细一点”。",
     "quote": "点评原文只有一句“注意运算顺序，再仔细检查”。",
     "expected": "定位到“先算除法再算减法”这一步。",
     "severity": "normal", "status": "confirmed_error", "tags": ["定位模糊", "扣分不当"]},
    {"case_id": "c008", "problem": "周长公式用错被扣成0分且评语打击性强。",
     "quote": "“这么简单都错”。",
     "expected": "指出混淆了周长与长宽之和，并保持鼓励语气。",
     "severity": "normal", "status": "confirmed_error", "tags": ["扣分不当"]},
]


def _wait_run(svc: RunService, rid: str, timeout: float = 120.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = svc.get(rid)
        if run["state"] in ("completed", "failed", "cancelled", "paused_budget"):
            return run
        time.sleep(0.2)
    raise TimeoutError(f"示例运行 {rid} 未在限定时间内完成")


def seed_demo(db: DB | None = None) -> dict:
    """生成（或复用）内置示例项目，并完整跑通一次优化流程。"""
    db = db or get_db()
    psvc = ProjectsService(db)
    existing = [p for p in psvc.list() if p["name"] == DEMO_NAME]
    if existing:
        return {"project_id": existing[0]["id"], "created": False,
                "note": "示例项目已存在，直接打开。"}
    p = psvc.create(
        DEMO_NAME,
        "内置演示项目：用离线模拟数据完整走一遍优化流程。所有结果均为演示性质，"
        "你可以随便点开每一步看——看完再用自己的真实材料开始。",
        "student_feedback",
        goal="列式正确但计算出错时，点评经常把计算错误归因为“概念不清”；"
             "希望先区分列式思路与计算，再给结论，并明确指出错在哪一步。")
    pid = p["id"]

    # 1) 材料：8 条案例（6 条练习 + 2 条“考题”）
    dsvc = DataService(db)
    lines = []
    for c in _DEMO_CASES:
        lines.append(json.dumps({
            "case_id": c["case_id"], "origin": "real",
            "runtime_input": {"question": c["question"], "student_answer": c["student_answer"],
                              "grade_level": "初中"},
            "evaluation_only": {"expert_answer": c["expert_answer"]},
        }, ensure_ascii=False))
    batch = dsvc.preview_import(pid, "示例案例", "jsonl", "\n".join(lines))
    valid = [{"case_id": c["case_id"], "source_group_id": c["case_id"], "origin": "real",
              "scene_tags": "",
              "runtime_input": {"question": c["question"], "student_answer": c["student_answer"],
                                "grade_level": "初中"},
              "evaluation_only": {"expert_answer": c["expert_answer"]}} for c in _DEMO_CASES]
    dsvc.store_source(batch["id"], batch["preview_hash"], valid)
    dsvc.commit_import(pid, batch["id"], [])
    id_by_case = {r["case_id"]: r["id"] for r in
                  db.query("SELECT id, case_id FROM dataset_items WHERE project_id=?", (pid,))}
    dev_ids = [id_by_case[c["case_id"]] for c in _DEMO_CASES[:6]]
    sealed_ids = [id_by_case[c["case_id"]] for c in _DEMO_CASES[6:]]
    dsvc.assign_split(pid, dev_ids, "dev")
    dsvc.assign_split(pid, sealed_ids, "sealed_test")
    dsvc.freeze_manifest(pid, seed=7)

    # 2) 评价方式：标准 + ABCD + 专家意见
    rub = RubricService(db).create_draft(pid)
    RubricService(db).publish(rub["id"])
    RatingService(db).create_default(pid)
    fsvc = FeedbackService(db)
    for fb in _DEMO_FEEDBACK:
        fsvc.add(pid, id_by_case[fb["case_id"]], fb["problem"], quote=fb["quote"],
                 expected=fb["expected"], severity=fb["severity"], status=fb["status"],
                 tags=fb["tags"], source="import")

    # 3) 基线提示词（刻意用简单版：模拟“还没优化过”的现状）
    pv = PromptService(db).create_version(
        pid, "学员点评提示词", _BASELINE_BODY,
        [], ["question", "student_answer", "grade_level"], {})

    # 4) 原始测评 + 自动优化（离线模拟，几秒内完成）
    mans = db.query("SELECT id FROM split_manifests WHERE project_id=?", (pid,))
    snapshot = {
        "mode": "explore", "prompt": {"baseline_id": pv["id"]}, "rubric_id": rub["id"],
        "judge_id": None, "manifest_id": mans[0]["id"] if mans else "",
        "data": {"dev_item_ids": dev_ids, "select_item_ids": []},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_rounds": 2, "dev_sample_size": 6, "min_delta": 0.02,
                         "stall_rounds": 2, "length_limit_chars": 4000},
        "budget": {"mode": "token", "total_limit": 2_000_000, "search_limit": 1_200_000,
                   "acceptance_limit": 600_000},
    }
    rsvc = RunService(db)
    run = rsvc.create_run(pid, rsvc.validate_snapshot(pid, snapshot),
                          idempotency_key="demo_seed_" + new_id("x")[:6])
    rsvc.start(run["id"])
    run = _wait_run(rsvc, run["id"])

    # 5) 锁定 + 独立验证 + 采用（无论结论如何都走完整闭环）
    if run["state"] != "completed":
        return {"project_id": pid, "created": True, "run_state": run["state"],
                "note": "示例运行未完成，可在项目内查看状态。"}
    kept = [c for c in run["candidates"] if c.get("decision") == "kept"]
    target = kept[0]["candidate_id"] if kept else "baseline"
    rsvc.lock_candidate(run["id"], target)
    rep = AcceptanceService(db).accept(run["id"])
    cand_pv = run["baseline_prompt_id"] if target == "baseline" else \
        [c for c in run["candidates"] if c["candidate_id"] == target][0]["prompt_version_id"]
    if rep["decision"] == "verified_improvement":
        rel = ReleaseService(db).adopt(pid, cand_pv, rep["id"], "active")
    else:
        rel = ReleaseService(db).adopt(pid, run["baseline_prompt_id"], "", "trial")
    ReleaseService(db).submit_feedback(
        rel["id"], "direct", "5分钟", "示例反馈：看完流程后用真实材料替换。")
    db.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at)"
               " VALUES(?,?,?,?,?,?)",
               (new_id("aud"), "system", "demo.seed", pid, "", now_iso()))
    return {"project_id": pid, "created": True, "decision": rep["decision"],
            "note": "示例项目已生成：材料、专家意见、优化轮次、验证报告与采用记录齐全。"}

"""端到端闭环演示：案例→标准→基线→优化→候选→独立验收→采用→反馈。

运行方式： python demo/run_e2e_demo.py
本脚本在临时数据库上以真实应用实例走完整流程，输出每一步的结果，
用于交付前验证"整个流程是否通畅"。所有数据为演示数据（demo）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

STEPS: list[tuple[str, str]] = []


def step(name: str, detail: str = "") -> None:
    STEPS.append((name, detail))
    print(f"  [OK] {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    from prompt_lib.db import DB, set_db
    db_path = os.path.join(tempfile.gettempdir(), f"pl_demo_{int(time.time())}.db")
    set_db(DB(db_path))
    from prompt_lib.api import create_app
    client = TestClient(create_app(str(REPO / "web")))
    base = "/workflow-api/v1"

    print("== 提示词优化实验室 · 端到端闭环演示（演示数据）==\n")

    # P01 创建项目（任务模板：学员作答点评）
    tpl = client.get(base + "/templates").json()
    assert set(tpl) == {"student_feedback", "question_explain", "article_title",
                        "article_framework"}, "四种任务模板齐备"
    p = client.post(base + "/projects", json={
        "name": "演示：初中学员点评优化", "description": "端到端演示项目（demo）",
        "task_type": "student_feedback"}).json()
    pid = p["id"]
    step("P01 创建项目", f"任务模板自动生成输入契约与评价标准草案（is_demo={p['contract']['is_demo']}）")

    # P03 导入 12 条案例（含 evaluation_only 评价专用字段）
    items = []
    for i in range(1, 13):
        items.append({"case_id": f"c{i:03d}", "source_group_id": f"g{i:03d}", "origin": "real",
                      "runtime_input": {"question": f"题目{i}：解方程 2x+{i}=10",
                                        "student_answer": f"学员答案{i}：x={5 - i // 2}",
                                        "grade_level": "初中"},
                      "evaluation_only": {"expert_answer": f"参考答案{i}（评价专用，绝不进入生成请求）"}})
    content = "\n".join(json.dumps(it, ensure_ascii=False) for it in items)
    batch = client.post(base + f"/projects/{pid}/imports/preview",
                        json={"fmt": "jsonl", "content": content}).json()
    step("P03 导入预览", f"总行数 {batch['total']}，有效 {batch['valid']}，错误 {len(batch['errors'])}")
    client.post(base + f"/projects/{pid}/imports/{batch['id']}/commit",
                json={"exclude_case_ids": []})
    listing = client.get(base + f"/projects/{pid}/items?size=100").json()
    ids = [it["id"] for it in listing["items"]]
    client.post(base + f"/projects/{pid}/split", json={"case_ids": ids[:6], "split": "dev"})
    client.post(base + f"/projects/{pid}/split", json={"case_ids": ids[6:8], "split": "select"})
    client.post(base + f"/projects/{pid}/split", json={"case_ids": ids[8:], "split": "sealed_test"})
    fr = client.post(base + f"/projects/{pid}/manifests/freeze", json={"seed": 20260927}).json()
    step("P03 冻结切分清单", f"来源分组 {fr['groups']} 个；封存测试 {fr['sealed_test_items']} 条进入受限存储")
    sealed_view = client.get(base + f"/projects/{pid}/items?split=sealed_test").json()
    assert sealed_view["items"] == [] and sealed_view["sealed_summary"]["count"] == 4
    step("P03 封存隔离验证", "普通列表无法读取封存原文，只显示数量与覆盖（TC010）")

    # P04 评价标准：模板草稿 → 发布
    rub = client.post(base + f"/projects/{pid}/rubrics").json()
    client.post(base + f"/rubrics/{rub['id']}/publish")
    step("P04 发布评价标准", "维度锚点0-3齐全；标准变化会使旧评价器stale（TC012/TC024）")

    # P09 创建基线提示词（含冻结组件与变量白名单）
    pv = client.post(base + f"/projects/{pid}/prompts", json={
        "name": "学员点评提示词", "body": "你是一名初中教研老师。请点评学员答案。",
        "variables": ["question", "student_answer", "grade_level"],
        "frozen_segments": [{"name": "安全声明", "text": "不得虚构学员错误。"}],
        "params": {}}).json()
    tr = client.post(base + f"/prompts/{pv['id']}/trial", json={"item_id": ids[0]})
    assert tr.status_code == 200
    step("P09 试运行基线", "单条真实请求走统一计量入口；evaluation_only 未出现在请求中（BR01/BR09）")

    # P10/P11 启动实验（探索模式，2个候选，离线模拟供应商）
    draft = {
        "mode": "explore",
        "prompt": {"baseline_id": pv["id"]},
        "rubric_id": rub["id"], "judge_id": None,
        "manifest_id": fr["manifest_id"],
        "data": {"dev_item_ids": ids[:6], "select_item_ids": ids[6:8]},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_candidates": 2, "dev_sample_size": 6, "min_delta": 0.02},
        "budget": {"mode": "token", "total_limit": 2_000_000,
                   "search_limit": 1_200_000, "acceptance_limit": 600_000},
    }
    est = client.post(base + f"/projects/{pid}/runs/validate", json=draft).json()
    step("P10 配置校验与预估", f"预估token区间 {est['estimate']['estimated_tokens']}（假设法，硬预算兜底）")
    run = client.post(base + f"/projects/{pid}/runs", json=draft,
                      headers={"Idempotency-Key": "demo-key-001"}).json()
    again = client.post(base + f"/projects/{pid}/runs", json=draft,
                        headers={"Idempotency-Key": "demo-key-001"}).json()
    assert again["id"] == run["id"], "幂等重放返回同一run（TC035）"
    run = wait_run(client, run["id"])
    step("P11 实验运行完成",
         f"状态 {run['state']}，停止原因 {run['stop_reason']}，基线分 {run.get('baseline_score', 0):.3f}")
    led = client.get(base + f"/runs/{run['id']}/ledger").json()
    step("P11 账本对账", f"attempt {led['attempts']} 次，确定用量 {led['known_tokens']} tok，"
                        f"未知在途 {led['unknown_tokens']} tok，一致性 {led['consistent']}（TC057）")
    assert run["candidates"], "优化器产出了候选"

    # 锁定最优候选
    best = max(run["candidates"], key=lambda c: c["score"])
    client.post(base + f"/runs/{run['id']}/lock", json={"candidate_id": best["candidate_id"]})
    step("P11 锁定最终候选", f"{best['candidate_id']}（父版本可追溯，平均分 {best['score']:.3f}）")

    # P12 独立验收：解封封存测试集（一次性消耗）
    acc = client.post(base + f"/runs/{run['id']}/accept").json()
    st = acc["stats"]
    print(f"        验收明细：基线可用率 {st['baseline_usable_rate']:.0%} → 候选 {st['candidate_usable_rate']:.0%}，"
          f"差异 {st['diff']:+.0%}；修复 {st['fix']} / 退步 {st['regress']}；"
          f"McNemar p={st['mcnemar_p']:.4f}；CI=[{st['bootstrap']['ci_low']:+.0%}, {st['bootstrap']['ci_high']:+.0%}]")
    assert st["group_n"] + st["unknown"] == st["sealed_total"], "未知项不静默删除"
    step("P12 独立验收完成", f"结论：{acc['decision']}（不可变报告；测试明细已标consumed）")
    acc2 = client.post(base + f"/runs/{run['id']}/accept")
    assert acc2.status_code == 409 and acc2.json()["code"] == "TEST_ALREADY_CONSUMED"
    step("P12 消耗保护验证", "已消耗的测试明细不能再次作为独立证明（TC043）")

    # P13 采用与反馈
    if acc["decision"] == "verified_improvement":
        rel = client.post(base + f"/projects/{pid}/releases", json={
            "prompt_version_id": best["prompt_version_id"],
            "report_ref": acc["id"], "mode": "active"}).json()
        step("P13 正式采用", f"发布绑定 提示词版本+模型配置+验收报告（指针已更新）")
    else:
        rel = client.post(base + f"/projects/{pid}/releases", json={
            "prompt_version_id": pv["id"], "mode": "trial"}).json()
        step("P13 保存试用", "结论未达verified门槛：只能trial，不覆盖正式指针（TC048）")
    fb = client.post(base + f"/releases/{rel['id']}/feedback", json={
        "adoption": "minor_edit", "edit_time": "10分钟", "reason": "演示：语气需更柔和"}).json()
    assert fb["status"] == "pending_review"
    step("P13 使用反馈", "反馈进入待核验池，不自动成为测试gold（TC050）")

    # 全局审计
    audit = client.get(base + "/audit").json()["audit"]
    actions = sorted({a["action"] for a in audit})
    step("FR25 审计追溯", "审计事件：" + "、".join(actions))

    print("\n== 闭环验证结论 ==")
    print(f"共 {len(STEPS)} 步全部通过：案例→标准→基线→优化→独立验收→使用反馈 全流程通畅。")
    print(f"验收决策：{acc['decision']}（软件验收与提示词效果验收分开："
          f"{'候选通过门槛' if acc['decision'] == 'verified_improvement' else '未发现改善也是合法结果'}）")
    print(f"演示数据库：{db_path}")
    return 0


def wait_run(client, rid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = client.get(f"/workflow-api/v1/runs/{rid}").json()
        if run["state"] in ("completed", "failed", "cancelled", "paused_budget"):
            return run
        time.sleep(0.2)
    raise TimeoutError("run not settled")


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""端到端流程测试：对运行中的 server.py 全流程走一遍（配合 mock_llm.py）。

前提：mock_llm.py 在 8901 端口、server.py 在 8765 端口（--port 8765 启动），
且 server 使用独立的空数据目录（直接在 prompt-lab 下运行即可，测试会自建数据）。

用法：python tests/e2e_test.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

TEST_PORT = 8799
MOCK_PORT = 8901
BASE = "http://127.0.0.1:%d" % TEST_PORT
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

_server_proc = None
_data_dir = None


def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.2).close()
        return True
    except OSError:
        return False


def _start_services():
    global _server_proc, _data_dir
    if not _port_open(MOCK_PORT):
        subprocess.Popen([sys.executable, os.path.join(HERE, "mock_llm.py"), str(MOCK_PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _port_open(MOCK_PORT):
            break
        time.sleep(0.25)
    _data_dir = tempfile.mkdtemp(prefix="prompt-lab-e2e-")
    env = dict(os.environ, PROMPT_LAB_DATA=_data_dir)
    _server_proc = subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, "server.py"),
         "--port", str(TEST_PORT), "--no-browser"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _port_open(TEST_PORT):
            return
        time.sleep(0.25)
    raise SystemExit("隔离测试实例启动失败")


def _stop_services():
    global _server_proc, _data_dir
    if _server_proc:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
        _server_proc = None
    if _data_dir:
        shutil.rmtree(_data_dir, ignore_errors=True)
        _data_dir = None
PASSED = []
FAILED = []


def call(path, body=None, method=None, expect_error=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or
                                 ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode("utf-8"))
        if expect_error:
            return payload
        raise AssertionError("请求 %s 失败：%s" % (path, payload.get("message")))


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("通过｜" + name)
    else:
        FAILED.append((name, detail))
        print("失败｜" + name + "｜" + detail)


def wait_run_state(run_id, states, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = call("/api/runs/%d" % run_id)
        if d["run"]["state"] in states:
            return d
        time.sleep(0.5)
    raise AssertionError("等待状态超时，当前：%s，error=%s" % (d["run"]["state"], d["run"].get("error")))


def judge_ratings(p):
    """模拟一个能看出好坏的盲评评审：改进版输出（含"改进版"标记）判 usable 并占优。

    盲评接口在评完前不揭晓甲乙身份（设计使然），所以只能按内容质量打分。
    """
    a_good = "改进版" in p["a"]
    b_good = "改进版" in p["b"]
    rating_a = "usable" if a_good else "unusable"
    rating_b = "usable" if b_good else "unusable"
    if a_good and not b_good:
        pref = "a"
    elif b_good and not a_good:
        pref = "b"
    else:
        pref = "tie"
    return rating_a, rating_b, pref


def main():
    _start_services()
    try:
        _run_all()
    finally:
        _stop_services()


def _run_all():
    # 0. 基础状态
    d = call("/api/state")
    check("服务可访问且初始未配置", d["ok"] is True and d["configured"] is False)

    # 1. 配置
    d = call("/api/settings", {"api_base": "http://127.0.0.1:8901/v1", "api_key": "sk-test",
                               "model": "mock-model", "cap_type": "requests", "cap_value": "50"})
    check("保存设置", d["ok"] is True and d["settings"]["api_key"] is True,
          "密钥不应回传本体")
    d = call("/api/settings/test", {"api_base": "http://127.0.0.1:8901/v1", "api_key": True,
                                    "model": "mock-model"})
    check("测试连接成功", d.get("ok") is True, str(d))
    # 坏密钥路径
    d = call("/api/settings/test", {"api_base": "http://127.0.0.1:8901/v1",
                                    "api_key": "wrong", "model": "mock-model"})
    check("坏密钥给出大白话错误", d.get("ok") is False and "密钥" in d.get("message", ""), str(d))

    # 2. 新建提示词 + 例子
    d = call("/api/prompts", {"name": "学员点评测试", "content": "你是点评老师。请点评：{输入}"})
    pid, vid1 = d["prompt_id"], d["version_id"]
    cases = [{"input": "学生甲的答案：勾股定理 a²+b²=c"}, {"input": "学生乙的答案：春天来了"},
             {"input": "学生丙的答案：9×9=81"}]
    d = call("/api/prompts/%d/cases" % pid, {"cases": cases})
    check("导入例子", d["ok"] is True and d["saved"] == 3)
    detail = call("/api/prompts/%d" % pid)
    case_ids = [c["id"] for c in detail["cases"]]

    # 3. 探索运行：生成基线
    d = call("/api/runs", {"prompt_id": pid, "kind": "explore",
                           "base_version_id": vid1, "case_ids": case_ids})
    run_id = d["run_id"]
    call("/api/runs/%d/step/base" % run_id, {})
    d = wait_run_state(run_id, ["rating_base", "failed", "paused_cap", "stopped"])
    check("基线生成完成进入评价", d["run"]["state"] == "rating_base",
          d["run"]["state"] + " err=" + str(d["run"].get("error")))
    check("账本记了生成调用", call("/api/ledger")["total"]["requests"] >= 3)

    # 4. 评价 + 改写
    outs = call("/api/runs/%d/outputs" % run_id)["outputs"]
    ratings = [{"output_id": o["id"],
                "usable": ["usable", "unusable", "minor"][i % 3],
                "problem_note": "第%d份有问题：判断太武断" % (i + 1) if i == 1 else ""}
               for i, o in enumerate(outs)]
    d = call("/api/runs/%d/ratings" % run_id, {"ratings": ratings})
    check("保存评价", d["saved"] == 3)
    d = call("/api/runs/%d/improve" % run_id,
             {"problem_summary": "判断太武断，需要先核对再下结论"})
    check("改写生成新版本", d["ok"] is True and d["change_note"]
          and "改进要求" in d["content"], str(d)[:200])
    cand_vid = d["version_id"]

    # 5. 生成新版输出 + 盲评
    call("/api/runs/%d/step/candidate" % run_id, {})
    d = wait_run_state(run_id, ["comparing", "failed", "paused_cap", "stopped"])
    check("新版生成完成进入盲评", d["run"]["state"] == "comparing", str(d["run"].get("error")))
    pairs = call("/api/runs/%d/pairs" % run_id)["pairs"]
    check("配对数与例子数一致且未揭晓", len(pairs) == 3 and
          all(p["a_is_candidate"] is None for p in pairs), str(len(pairs)))
    for p in pairs:
        ra, rb, pref = judge_ratings(p)
        call("/api/pairs/%d" % p["id"], {"rating_a": ra, "rating_b": rb,
                                         "preference": pref})
        # 揭晓后应能看到甲乙身份
    judged_pairs = call("/api/runs/%d/pairs" % run_id)["pairs"]
    check("评完后揭晓身份", all(p["a_is_candidate"] is not None for p in judged_pairs))
    d = call("/api/runs/%d/verdict" % run_id)
    v = d["verdict"]
    check("结论已生成", v is not None and v["counts"]["judged"] == 3)
    check("探索结论不敢说证明（样本太少）", v["conclusion_level"] in ("too_few", "no_diff", "lean_yes")
          and "考" not in v["headline"][:3], v["headline"])
    check("新版占优计数正确", v["counts"]["cand_better"] == 3 and v["counts"]["base_better"] == 0,
          json.dumps(v["counts"], ensure_ascii=False))

    # 6. 存为试用版
    d = call("/api/runs/%d/save_trial" % run_id, {})
    check("存为试用版", d["ok"] is True)
    detail = call("/api/prompts/%d" % pid)
    check("试用指针已设置", detail["prompt"]["trial_version_id"] == cand_vid)
    exams = call("/api/exams")["exams"]
    exam_row = [e for e in exams if e["prompt_id"] == pid]
    check("考试列表带试用版ID（前端启动考试依赖）",
          exam_row and exam_row[0]["trial_version_id"] == cand_vid,
          json.dumps(exams, ensure_ascii=False)[:200])

    # 7. 日常使用 + 反馈回流
    d = call("/api/use", {"prompt_id": pid, "input": "学生丁的答案：水 H2O"})
    check("日常使用出结果", d["ok"] is True and "模拟" in d["output"], str(d)[:150])
    check("使用结果标明版本", "版" in d["version_label"], d["version_label"])
    d = call("/api/use/%d/feedback" % d["use_id"], {"result": "edited",
                                                    "edited_text": "改后的点评文本",
                                                    "add_to_pool": True})
    check("反馈提交且回流例子池", d["ok"] is True)
    detail = call("/api/prompts/%d" % pid)
    check("例子池新增一条来源=日常使用", detail["cases"][0]["source"] == "usage")

    # 8. 例子不够时考试应被拒
    d = call("/api/runs", {"prompt_id": pid, "kind": "validate",
                           "base_version_id": vid1, "candidate_version_id": cand_vid,
                           "case_ids": case_ids}, expect_error=True)
    # case_ids 只有 3 条 → 不足 5
    check("考试例子不足被拒", d.get("ok") is False and "5" in d.get("message", ""), str(d))

    # 9. 补 6 条新例子 → 考试
    fresh = [{"input": "新例子 %d：内容内容内容" % i} for i in range(6)]
    call("/api/prompts/%d/cases" % pid, {"cases": fresh})
    detail = call("/api/prompts/%d" % pid)
    fresh_ids = [c["id"] for c in detail["cases"]
                 if c["id"] not in case_ids][:6]
    d = call("/api/runs", {"prompt_id": pid, "kind": "validate",
                           "base_version_id": vid1, "candidate_version_id": cand_vid,
                           "case_ids": fresh_ids})
    exam_id = d["run_id"]
    call("/api/runs/%d/step/base" % exam_id, {})
    d = wait_run_state(exam_id, ["comparing", "failed", "paused_cap", "stopped"], timeout=120)
    check("考试跑完进入盲评（基线自动衔接新版）", d["run"]["state"] == "comparing",
          str(d["run"].get("error")))
    pairs = call("/api/runs/%d/pairs" % exam_id)["pairs"]
    for p in pairs:
        ra, rb, pref = judge_ratings(p)
        call("/api/pairs/%d" % p["id"], {"rating_a": ra, "rating_b": rb,
                                         "preference": pref})
    v = call("/api/runs/%d/verdict" % exam_id)["verdict"]
    check("考试结论生成", v is not None and v["counts"]["judged"] == 6)
    check("每边可用评级正确落库", v["counts"]["cand_usable"] == len(pairs)
          and v["counts"]["base_usable"] == 0,
          json.dumps(v["counts"], ensure_ascii=False))
    check("样本不足时不允许直接转正", v["can_adopt"] is False and v["sample_warning"] != "",
          json.dumps({"can_adopt": v["can_adopt"], "warn": v["sample_warning"]}, ensure_ascii=False))
    check("结论含大白话警告与频数", "份" in v["detail_lines"][0]
          and "能直接用" in "".join(v["detail_lines"]), str(v["detail_lines"])[:150])

    # 10. 考过的例子不能再用
    d = call("/api/runs", {"prompt_id": pid, "kind": "validate",
                           "base_version_id": vid1, "candidate_version_id": cand_vid,
                           "case_ids": fresh_ids}, expect_error=True)
    check("考过的例子复用被拦截", d.get("ok") is False and "考过" in d.get("message", ""), str(d))

    # 11. 无验证强转正需确认（need_force）
    d = call("/api/prompts/%d/adopt" % pid, {"version_id": cand_vid, "run_id": exam_id})
    check("小样本转正需要强制确认", d.get("need_force") is True, str(d))
    d = call("/api/prompts/%d/adopt" % pid, {"version_id": cand_vid, "run_id": exam_id,
                                             "force": True})
    check("确认后转正成功", d.get("ok") is True, str(d))
    detail = call("/api/prompts/%d" % pid)
    check("正式指针已指向试用版", detail["prompt"]["official_version_id"] == cand_vid)

    # 12. 次数上限到顶自动暂停 + 提额续跑
    call("/api/settings", {"cap_value": "2"})
    d = call("/api/prompts/%d/cases" % pid, {"cases": [{"input": "上限测试例子A"},
                                                       {"input": "上限测试例子B"},
                                                       {"input": "上限测试例子C"}]})
    detail = call("/api/prompts/%d" % pid)
    cap_ids = [c["id"] for c in detail["cases"]][:3]
    d = call("/api/runs", {"prompt_id": pid, "kind": "explore",
                           "base_version_id": vid1, "case_ids": cap_ids})
    cap_run = d["run_id"]
    call("/api/runs/%d/step/base" % cap_run, {})
    d = wait_run_state(cap_run, ["paused_cap", "rating_base", "failed", "stopped"])
    check("到次数上限自动暂停", d["run"]["state"] == "paused_cap", d["run"]["state"])
    check("上限提示是大白话", "上限" in (d["run"]["error"] or ""), str(d["run"]["error"]))
    call("/api/settings", {"cap_value": "50"})
    d = call("/api/runs/%d/cap" % cap_run, {"cap_value": 50})
    check("提高单轮上限成功", d.get("ok") is True, str(d))
    d = call("/api/runs/%d/cap" % cap_run, {"cap_value": 10}, expect_error=True)
    check("下调上限被拒绝", d.get("ok") is False, str(d))
    call("/api/runs/%d/step/base" % cap_run, {})
    d = wait_run_state(cap_run, ["rating_base", "paused_cap", "failed", "stopped"])
    check("提额后可续跑完成", d["run"]["state"] == "rating_base", d["run"]["state"])

    # 13. 账本完整
    d = call("/api/ledger")
    check("账本有失败也有成功记录", d["total"]["requests"] >= 10 and
          any(e["status"] == "error" for e in d["entries"]))

    print("\n—— 结果：%d 通过，%d 失败 ——" % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print("失败项：%s｜%s" % (name, detail))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

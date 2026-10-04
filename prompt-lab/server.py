# -*- coding: utf-8 -*-
"""
提示词优化实验室 · 单机版 v0.1
纯 Python 标准库实现，零第三方依赖。仅绑定 127.0.0.1，供本机使用。

启动：python server.py  （或双击 启动.bat）
自检：python server.py --check
"""
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import random
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "lab.db")

DEFAULT_SETTINGS = {
    "api_base": "",        # 例如 https://api.openai.com/v1
    "api_key": "",
    "model": "",
    "cap_type": "requests",   # requests=次数上限；money=金额上限（需先填单价）
    "cap_value": "40",        # 每轮优化的上限
    "price_in": "",           # 元 / 百万输入token，可留空
    "price_out": "",          # 元 / 百万输出token，可留空
}

WORKER_LOCKS = {}
WORKER_LOCKS_GUARD = threading.Lock()

# ---------------------------------------------------------------- 基础工具

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings(
          k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS prompts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL, note TEXT DEFAULT '',
          official_version_id INTEGER, trial_version_id INTEGER,
          use_pref TEXT DEFAULT 'auto',
          created_at TEXT, archived INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          prompt_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
          content TEXT NOT NULL, label TEXT DEFAULT '',
          origin TEXT DEFAULT 'manual', parent_version_id INTEGER,
          note TEXT DEFAULT '', created_at TEXT,
          UNIQUE(prompt_id, version_no));
        CREATE TABLE IF NOT EXISTS cases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          prompt_id INTEGER NOT NULL,
          input_text TEXT NOT NULL, reference_text TEXT DEFAULT '',
          source TEXT DEFAULT 'pasted', used_in TEXT DEFAULT '[]',
          created_at TEXT);
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          prompt_id INTEGER NOT NULL, kind TEXT NOT NULL,
          base_version_id INTEGER, candidate_version_id INTEGER,
          state TEXT DEFAULT 'ready',
          cap_type TEXT DEFAULT 'requests', cap_value REAL DEFAULT 40,
          spent_requests INTEGER DEFAULT 0, spent_tokens_in INTEGER DEFAULT 0,
          spent_tokens_out INTEGER DEFAULT 0, spent_cost REAL DEFAULT 0,
          problem_summary TEXT DEFAULT '', verdict TEXT, error TEXT,
          case_ids TEXT DEFAULT '[]',
          created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS outputs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL, case_id INTEGER NOT NULL, version_id INTEGER NOT NULL,
          content TEXT, status TEXT DEFAULT 'ok', error TEXT,
          tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
          model TEXT DEFAULT '', usable TEXT, problem_note TEXT DEFAULT '',
          created_at TEXT, UNIQUE(run_id, case_id, version_id));
        CREATE TABLE IF NOT EXISTS pairs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL, case_id INTEGER NOT NULL,
          output_a_id INTEGER NOT NULL, output_b_id INTEGER NOT NULL,
          a_is_candidate INTEGER NOT NULL,
          rating_a TEXT, rating_b TEXT, preference TEXT, judged_at TEXT,
          created_at TEXT);
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id INTEGER,
          purpose TEXT, model TEXT DEFAULT '',
          prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
          est_cost REAL, status TEXT DEFAULT 'ok', detail TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS usage_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          prompt_id INTEGER NOT NULL, version_id INTEGER,
          input_text TEXT NOT NULL, output_text TEXT,
          status TEXT DEFAULT 'ok', error TEXT,
          tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
          feedback TEXT, edited_text TEXT, case_id INTEGER, created_at TEXT);
        """
    )
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()


def get_settings(conn):
    rows = conn.execute("SELECT k, v FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    for r in rows:
        s[r["k"]] = r["v"]
    return s


def api_ready(s):
    return bool(s.get("api_base") and s.get("api_key") and s.get("model"))


def unit_price(s):
    """返回 (输入单价, 输出单价) 元/百万token；未填返回 None"""
    try:
        pi = float(s["price_in"]) if str(s.get("price_in", "")).strip() else None
        po = float(s["price_out"]) if str(s.get("price_out", "")).strip() else None
        return pi, po
    except (TypeError, ValueError):
        return None, None


def est_cost_of(s, tokens_in, tokens_out):
    pi, po = unit_price(s)
    if pi is None and po is None:
        return None
    cost = 0.0
    if pi is not None:
        cost += tokens_in / 1e6 * pi
    if po is not None:
        cost += tokens_out / 1e6 * po
    return round(cost, 6)

# ---------------------------------------------------------------- LLM 调用

def _post_chat(settings, payload, timeout):
    base = settings["api_base"].strip().rstrip("/")
    url = base + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + settings["api_key"].strip(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def friendly_llm_error(e):
    if isinstance(e, urllib.error.HTTPError):
        code = e.code
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        if code in (401, 403):
            return "密钥不对或没有权限（错误码 %d）。请到设置里重新复制密钥。%s" % (code, detail)
        if code == 404:
            return "接口地址没找到（404）。检查地址结尾，一般需要以 /v1 结尾。%s" % detail
        if code == 429:
            return "接口限流了（429），请等一会儿再试。%s" % detail
        if code >= 500:
            return "模型服务暂时出错（%d），稍后再试。%s" % (code, detail)
        return "接口返回错误（%d）。%s" % (code, detail)
    if isinstance(e, urllib.error.URLError):
        return "连不上接口地址。请检查网络，以及地址是否写对（一般以 /v1 结尾）。"
    if isinstance(e, TimeoutError):
        return "连接超时了，请稍后再试。"
    return "调用失败：%s" % e


def call_llm(settings, messages, max_tokens=2048, temperature=0.5, timeout=180):
    payload = {
        "model": settings["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last_err = None
    for attempt in range(2):  # 429/超时重试一次
        try:
            resp = _post_chat(settings, payload, timeout)
            content = ""
            try:
                content = resp["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                pass
            usage = resp.get("usage") or {}
            return {
                "ok": True,
                "content": content,
                "tokens_in": int(usage.get("prompt_tokens") or 0),
                "tokens_out": int(usage.get("completion_tokens") or 0),
            }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError, KeyError) as e:
            last_err = e
            retryable = isinstance(e, urllib.error.HTTPError) and (
                e.code == 429 or e.code >= 500)
            if attempt == 0 and retryable:
                time.sleep(3)
                continue
            break
    return {"ok": False, "error": friendly_llm_error(last_err),
            "tokens_in": 0, "tokens_out": 0}

# ---------------------------------------------------------------- 提示词渲染

def render_prompt(content, input_text):
    """{输入} 占位符可选；没有占位符就把输入附在末尾。"""
    if "{输入}" in content:
        return content.replace("{输入}", input_text)
    return content + "\n\n【本次输入】\n" + input_text

# ---------------------------------------------------------------- 账本与上限

def ledger_add(conn, run_id, purpose, model, tokens_in, tokens_out, est_cost,
               status="ok", detail=""):
    conn.execute(
        "INSERT INTO ledger(ts, run_id, purpose, model, prompt_tokens,"
        " completion_tokens, est_cost, status, detail) VALUES(?,?,?,?,?,?,?,?,?)",
        (now(), run_id, purpose, model, tokens_in, tokens_out, est_cost, status, detail))


def run_refresh_spent(conn, run_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) to_,"
        " COUNT(*) n FROM outputs WHERE run_id=? AND status='ok'",
        (run_id,)).fetchone()
    return row["n"], row["ti"], row["to_"]


def cap_reached(conn, run, settings):
    """检查一轮优化的上限。返回 (是否到顶, 大白话原因)"""
    cap_type = run["cap_type"]
    cap_value = float(run["cap_value"] or 0)
    if cap_type == "requests":
        n, _, _ = run_refresh_spent(conn, run["id"])
        if cap_value > 0 and n >= cap_value:
            return True, "这轮优化的调用次数到了上限（%d 次）。" % int(cap_value)
        return False, ""
    # 金额上限：按已花估算 + 下一次的保守估计
    pi, po = unit_price(settings)
    if pi is None and po is None:
        return False, ""  # 没单价时金额模式无法估算，不拦截（前端已引导用次数模式）
    rows = conn.execute(
        "SELECT tokens_in, tokens_out FROM outputs WHERE run_id=? AND status='ok'",
        (run["id"],)).fetchall()
    spent = sum(est_cost_of(settings, r["tokens_in"], r["tokens_out"]) or 0 for r in rows)
    next_est = 2048 / 1e6 * (po or 0) + 4000 / 1e6 * (pi or 0)
    if cap_value > 0 and spent + next_est > cap_value:
        return True, "这轮优化的花费估算到了上限（约 %.2f 元）。" % cap_value
    return False, ""

# ---------------------------------------------------------------- 后台生成

def get_run(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def gen_worker(run_id, version_id):
    lock = WORKER_LOCKS.get(run_id)
    if lock is None:
        with WORKER_LOCKS_GUARD:
            lock = WORKER_LOCKS.setdefault(run_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    chain_version = None
    try:
        chain_version = _gen_worker_inner(run_id, version_id)
    finally:
        lock.release()
    if chain_version:  # 锁释放后再衔接下一侧，避免拿不到锁而卡住
        threading.Thread(target=gen_worker, args=(run_id, chain_version),
                         daemon=True).start()


def _gen_worker_inner(run_id, version_id):
    conn = db()
    try:
        run = get_run(conn, run_id)
        if run is None:
            return
        settings = get_settings(conn)
        if not api_ready(settings):
            conn.execute("UPDATE runs SET state='failed', error=?, updated_at=? WHERE id=?",
                         ("还没配置 AI 接口。请先到「设置」里填好接口地址、密钥和模型名。", now(), run_id))
            conn.commit()
            return
        kind = run["kind"]
        selected_ids = set(json.loads(run["case_ids"] or "[]"))
        side_label = "基线" if version_id == run["base_version_id"] else "新版"
        cases = conn.execute(
            "SELECT * FROM cases WHERE prompt_id=? ORDER BY id", (run["prompt_id"],)).fetchall()
        cases = [c for c in cases if c["id"] in selected_ids] if selected_ids else cases
        version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if version is None:
            conn.execute("UPDATE runs SET state='failed', error='找不到提示词版本。', updated_at=? WHERE id=?",
                         (now(), run_id))
            conn.commit()
            return
        failed_hard = False
        for case in cases:
            conn2 = db()
            run = get_run(conn2, run_id)
            if run["state"] in ("stopping", "stopped"):
                conn2.execute("UPDATE runs SET state='stopped', updated_at=? WHERE id=?",
                              (now(), run_id))
                conn2.commit()
                conn2.close()
                return
            hit, reason = cap_reached(conn2, run, settings)
            if hit:
                conn2.execute("UPDATE runs SET state='paused_cap', error=?, updated_at=? WHERE id=?",
                              (reason + " 已完成的部分都保留着。提高上限后点「继续」就行。", now(), run_id))
                conn2.commit()
                conn2.close()
                return
            existing = conn2.execute(
                "SELECT id FROM outputs WHERE run_id=? AND case_id=? AND version_id=?",
                (run_id, case["id"], version_id)).fetchone()
            if existing:
                conn2.close()
                continue
            rendered = render_prompt(version["content"], case["input_text"])
            result = call_llm(settings, [{"role": "user", "content": rendered}],
                              max_tokens=2048, temperature=0.5)
            est = est_cost_of(settings, result["tokens_in"], result["tokens_out"]) \
                if result["ok"] else None
            if result["ok"] and not result["content"].strip():
                result = {"ok": False, "error": "模型返回了空内容。", "tokens_in": result["tokens_in"],
                          "tokens_out": result["tokens_out"]}
            conn2.execute(
                "INSERT OR REPLACE INTO outputs(run_id, case_id, version_id, content, status,"
                " error, tokens_in, tokens_out, model, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, case["id"], version_id, result.get("content"),
                 "ok" if result["ok"] else "error", result.get("error"),
                 result["tokens_in"], result["tokens_out"], settings["model"], now()))
            ledger_add(conn2, run_id, "优化·生成（%s）" % side_label, settings["model"],
                       result["tokens_in"], result["tokens_out"], est,
                       "ok" if result["ok"] else "error", (result.get("error") or "")[:200])
            if not result["ok"] and re.search(r"密钥|连不上|401|403", result.get("error") or ""):
                failed_hard = True
            conn2.commit()
            conn2.close()
        # 本侧完成：推进状态
        conn3 = db()
        run = get_run(conn3, run_id)
        if run["state"] in ("stopping", "stopped"):
            conn3.execute("UPDATE runs SET state='stopped', updated_at=? WHERE id=?",
                          (now(), run_id))
            conn3.commit()
            conn3.close()
            return
        if failed_hard:
            conn3.execute("UPDATE runs SET state='failed', error='接口连不上或密钥不对，已停止。请到设置里检查后再继续。',"
                          " updated_at=? WHERE id=?", (now(), run_id))
            conn3.commit()
            conn3.close()
            return
        if version_id == run["base_version_id"]:
            if kind == "validate":
                conn3.execute("UPDATE runs SET state='generating_candidate', updated_at=? WHERE id=?",
                              (now(), run_id))
                conn3.commit()
                conn3.close()
                # 返回要衔接的版本，由 gen_worker 在释放锁后再启动
                return run["candidate_version_id"]
            conn3.execute("UPDATE runs SET state='rating_base', updated_at=? WHERE id=?",
                          (now(), run_id))
        else:
            make_pairs(conn3, run_id)
            conn3.execute("UPDATE runs SET state='comparing', updated_at=? WHERE id=?",
                          (now(), run_id))
        conn3.commit()
        conn3.close()
    finally:
        conn.close()


def make_pairs(conn, run_id):
    run = get_run(conn, run_id)
    selected_ids = set(json.loads(run["case_ids"] or "[]"))
    cases = conn.execute(
        "SELECT * FROM cases WHERE prompt_id=? ORDER BY id", (run["prompt_id"],)).fetchall()
    cases = [c for c in cases if c["id"] in selected_ids] if selected_ids else cases
    for case in cases:
        exist = conn.execute("SELECT id FROM pairs WHERE run_id=? AND case_id=?",
                             (run_id, case["id"])).fetchone()
        if exist:
            continue
        base_out = conn.execute(
            "SELECT id, status FROM outputs WHERE run_id=? AND case_id=? AND version_id=?",
            (run_id, case["id"], run["base_version_id"])).fetchone()
        cand_out = conn.execute(
            "SELECT id, status FROM outputs WHERE run_id=? AND case_id=? AND version_id=?",
            (run_id, case["id"], run["candidate_version_id"])).fetchone()
        if not base_out or not cand_out:
            continue
        if base_out["status"] != "ok" or cand_out["status"] != "ok":
            continue
        a_is_cand = random.random() < 0.5
        out_a = cand_out["id"] if a_is_cand else base_out["id"]
        out_b = base_out["id"] if a_is_cand else cand_out["id"]
        conn.execute(
            "INSERT INTO pairs(run_id, case_id, output_a_id, output_b_id, a_is_candidate, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, case["id"], out_a, out_b, 1 if a_is_cand else 0, now()))

# ---------------------------------------------------------------- 统计与结论

def exact_binom_two_sided(k, n):
    """双侧精确二项检验 p 值；n=0 返回 None"""
    if n <= 0:
        return None
    p_obs = math.comb(n, k) / (2 ** n)
    total = 0.0
    for i in range(n + 1):
        p_i = math.comb(n, i) / (2 ** n)
        if p_i <= p_obs * (1 + 1e-9):
            total += p_i
    return min(1.0, total)


def compute_verdict(conn, run):
    run_id = run["id"]
    pairs = conn.execute(
        "SELECT p.*, o_a.usable ua, o_b.usable ub FROM pairs p"
        " JOIN outputs o_a ON o_a.id=p.output_a_id"
        " JOIN outputs o_b ON o_b.id=p.output_b_id"
        " WHERE p.run_id=? ORDER BY p.id", (run_id,)).fetchall()
    judged = [p for p in pairs if p["rating_a"] and p["rating_b"]]
    cand_better = base_better = tie = unknown_pref = 0
    base_usable = cand_usable = improved = regressed = 0
    base_minor = cand_minor = 0
    improved_ids, regressed_ids = [], []
    cand_usable = base_usable = 0
    cand_minor = base_minor = 0
    for p in judged:
        ua, ub, is_a_cand = p["ua"], p["ub"], bool(p["a_is_candidate"])
        cand_u = ua == "usable" if is_a_cand else ub == "usable"
        base_u = ub == "usable" if is_a_cand else ua == "usable"
        cand_m = ua == "minor" if is_a_cand else ub == "minor"
        base_m = ub == "minor" if is_a_cand else ua == "minor"
        cand_usable += 1 if cand_u else 0
        base_usable += 1 if base_u else 0
        cand_minor += 1 if cand_m else 0
        base_minor += 1 if base_m else 0
        if cand_u and not base_u:
            improved += 1
            improved_ids.append(p["case_id"])
        if base_u and not cand_u:
            regressed += 1
            regressed_ids.append(p["case_id"])
        pref = p["preference"]
        if pref in ("a", "b"):
            cand_win = (pref == "a") == is_a_cand
            if cand_win:
                cand_better += 1
            else:
                base_better += 1
        elif pref == "tie":
            tie += 1
        else:
            unknown_pref += 1
    n = len(judged)
    p_pref = exact_binom_two_sided(cand_better, cand_better + base_better)
    disc = improved + regressed
    p_usable = exact_binom_two_sided(min(improved, regressed), disc) if disc else None

    kind = run["kind"]
    lines = []
    lines.append("一共比较了 %d 份。" % n)
    lines.append("哪边更好：新版 %d 份、原版 %d 份、差不多 %d 份、说不清 %d 份。"
                 % (cand_better, base_better, tie, unknown_pref))
    lines.append("能直接用的份数：原版 %d 份 → 新版 %d 份（修好 %d 份，弄坏 %d 份）。"
                 % (base_usable, cand_usable, improved, regressed))
    if base_minor or cand_minor:
        lines.append("「改改能用」：原版 %d 份、新版 %d 份。" % (base_minor, cand_minor))

    def level():
        if n < 5:
            return "too_few"
        if cand_better + base_better < 4:
            return "no_diff"
        if p_pref is not None and p_pref < 0.05:
            return "proof" if cand_better > base_better else "worse"
        if cand_better > base_better * 2:
            return "lean_yes"
        if base_better > cand_better * 2:
            return "lean_no"
        return "no_diff"

    lv = level()
    if kind == "explore":
        headline = {
            "too_few": "比较的份数太少，只能当参考。",
            "proof": "看起来新版更好——但这只是探索，份数还不够下结论。",
            "lean_yes": "看起来新版更好——但这只是探索，还不能算证明。",
            "lean_no": "看起来新版不占优——这只是探索，仅供参考。",
            "no_diff": "这轮没比出明显差别，仅供参考。",
        }[lv]
        tail = ("新版还没考过试，最多当参考。想更有把握，攒够例子后用「考个试」验证。")
    else:
        headline = {
            "too_few": "比较的份数太少（少于 5 份），说明不了问题。",
            "proof": "新版确实更好——碰运气出现这种结果的概率不到 5%。" if cand_better > base_better else "",
            "lean_yes": "新版看起来好一些，但还没到「不太可能是碰运气」的程度。建议再攒些例子。",
            "lean_no": "新版看起来不占优，建议保留原版。",
            "no_diff": "没比出明显差别：新版没有比原版更好。",
            "worse": "新版更差，建议保留原版。",
        }[lv]
        tail = "通过后可以点「转正」；不通过就保留原版，试用版继续改进。" if lv in ("proof", "lean_yes") \
            else "保留原版没有损失，已完成的比较都记录在案。"
    if p_pref is not None and n >= 5:
        pct = ("%.1f" % (p_pref * 100)).rstrip("0").rstrip(".")
        lines.append("如果是碰运气，出现这种「谁更好」分布的概率约 %s%%。%s"
                     % (pct, "（低于 5% 才算「不太可能是碰运气」）"))
    can_adopt = (kind == "validate") and lv in ("proof",) and n >= 15
    sample_warning = ""
    if kind == "validate" and n < 15:
        sample_warning = ("这次只比了 %d 份。经验上至少 15 份才能粗略判断，20 份以上更可靠；"
                          "想发现小幅提升需要更多。所以这次先不能转正——攒够例子再考一次更稳妥。" % n)
    verdict = {
        "headline": headline,
        "tail": tail,
        "detail_lines": lines,
        "conclusion_level": lv,
        "counts": {"judged": n, "pairs_total": len(pairs),
                   "cand_better": cand_better, "base_better": base_better,
                   "tie": tie, "unknown_pref": unknown_pref,
                   "base_usable": base_usable, "cand_usable": cand_usable,
                   "base_minor": base_minor, "cand_minor": cand_minor,
                   "improved": improved, "regressed": regressed},
        "p_pref": p_pref, "p_usable": p_usable,
        "improved_case_ids": improved_ids, "regressed_case_ids": regressed_ids,
        "can_adopt": can_adopt, "sample_warning": sample_warning,
    }
    return verdict

# ---------------------------------------------------------------- 校验辅助

def valid_usable(v):
    return v in ("usable", "minor", "unusable", "unknown")


def valid_pref(v):
    return v in ("a", "b", "tie", "unknown")

# ---------------------------------------------------------------- HTTP 服务

API_ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        API_ROUTES.append((method, rx, fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, msg, code=400):
        self.msg = msg
        self.code = code


def jbody(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise ApiError("请求内容不是合法的 JSON。", 400)


# ---- 设置 ----

@route("GET", r"/api/state")
def api_state(handler, m):
    conn = db()
    s = get_settings(conn)
    n_prompts = conn.execute("SELECT COUNT(*) c FROM prompts WHERE archived=0").fetchone()["c"]
    conn.close()
    return {"ok": True, "configured": api_ready(s), "prompts": n_prompts,
            "version": "0.1"}


@route("GET", r"/api/settings")
def api_get_settings(handler, m):
    conn = db()
    s = get_settings(conn)
    s["api_key"] = bool(s["api_key"])  # 不回传密钥本体
    conn.close()
    return {"ok": True, "settings": s}


@route("POST", r"/api/settings")
def api_set_settings(handler, m):
    b = jbody(handler)
    allowed = set(DEFAULT_SETTINGS) - {"api_key"}
    conn = db()
    for k in allowed:
        if k in b:
            conn.execute("INSERT INTO settings(k,v) VALUES(?,?)"
                         " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(b[k])))
    if "api_key" in b:
        conn.execute("INSERT INTO settings(k,v) VALUES('api_key',?)"
                     " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     ("",) if b["api_key"] is True else (str(b["api_key"]),))
    conn.commit()
    conn.close()
    return api_get_settings(handler, m)


@route("POST", r"/api/settings/test")
def api_test_settings(handler, m):
    b = jbody(handler)
    conn = db()
    s = get_settings(conn)
    conn.close()
    if "api_base" in b:
        s["api_base"] = str(b["api_base"])
    if "api_key" in b and b["api_key"] is not True:
        s["api_key"] = str(b["api_key"])
    if "model" in b:
        s["model"] = str(b["model"])
    if not (s["api_base"] and s["api_key"] and s["model"]):
        return {"ok": False, "message": "请先填全：接口地址、API 密钥、模型名。"}
    t0 = time.time()
    result = call_llm(s, [{"role": "user", "content": "请只回复两个字：正常"}],
                      max_tokens=16, temperature=0, timeout=30)
    conn = db()
    ledger_add(conn, None, "测试连接", s["model"], result["tokens_in"],
               result["tokens_out"], est_cost_of(s, result["tokens_in"], result["tokens_out"]),
               "ok" if result["ok"] else "error", (result.get("error") or "")[:200])
    conn.commit()
    conn.close()
    if result["ok"]:
        return {"ok": True, "message": "连接正常，模型能出字。可以开始用了。（耗时 %.1f 秒）"
                % (time.time() - t0)}
    return {"ok": False, "message": result["error"]}

# ---- 提示词 ----

def prompt_row(conn, pid):
    row = conn.execute("SELECT * FROM prompts WHERE id=? AND archived=0", (pid,)).fetchone()
    if not row:
        raise ApiError("找不到这条提示词。", 404)
    return row


def version_row(conn, vid, pid=None):
    row = conn.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
    if not row or (pid is not None and row["prompt_id"] != pid):
        raise ApiError("找不到这个提示词版本。", 404)
    return row


def add_version(conn, pid, content, label, origin, note="", parent=None):
    mx = conn.execute("SELECT COALESCE(MAX(version_no),0) m FROM versions WHERE prompt_id=?",
                      (pid,)).fetchone()["m"]
    cur = conn.execute(
        "INSERT INTO versions(prompt_id, version_no, content, label, origin, parent_version_id,"
        " note, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (pid, mx + 1, content, label, origin, parent, note, now()))
    return cur.lastrowid


@route("GET", r"/api/prompts")
def api_list_prompts(handler, m):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM prompts WHERE archived=0 ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        n_cases = conn.execute("SELECT COUNT(*) c FROM cases WHERE prompt_id=?",
                               (r["id"],)).fetchone()["c"]
        n_usage = conn.execute("SELECT COUNT(*) c FROM usage_log WHERE prompt_id=?",
                               (r["id"],)).fetchone()["c"]
        v_off = v_trial = None
        if r["official_version_id"]:
            v_off = conn.execute("SELECT version_no FROM versions WHERE id=?",
                                 (r["official_version_id"],)).fetchone()
        if r["trial_version_id"]:
            v_trial = conn.execute("SELECT version_no FROM versions WHERE id=?",
                                   (r["trial_version_id"],)).fetchone()
        out.append({"id": r["id"], "name": r["name"], "note": r["note"],
                    "cases": n_cases, "usage": n_usage,
                    "official_v": v_off["version_no"] if v_off else None,
                    "trial_v": v_trial["version_no"] if v_trial else None})
    conn.close()
    return {"ok": True, "prompts": out}


@route("POST", r"/api/prompts")
def api_create_prompt(handler, m):
    b = jbody(handler)
    name = str(b.get("name") or "").strip() or "未命名提示词"
    content = str(b.get("content") or "").strip()
    if not content:
        raise ApiError("提示词内容不能为空。把你在用的提示词原样贴进来就行。")
    if len(content) > 20000:
        raise ApiError("提示词太长了（超过 2 万字）。先精简一下，或把固定说明放到例子里。")
    conn = db()
    cur = conn.execute(
        "INSERT INTO prompts(name, note, created_at) VALUES(?,?,?)",
        (name, str(b.get("note") or ""), now()))
    pid = cur.lastrowid
    vid = add_version(conn, pid, content, "原版", "import", "最初贴进来的版本")
    conn.commit()
    conn.close()
    return {"ok": True, "prompt_id": pid, "version_id": vid}


@route("GET", r"/api/prompts/(\d+)")
def api_get_prompt(handler, m):
    pid = int(m.group(1))
    conn = db()
    r = prompt_row(conn, pid)
    versions = conn.execute(
        "SELECT id, version_no, label, origin, note, created_at, LENGTH(content) sz"
        " FROM versions WHERE prompt_id=? ORDER BY version_no", (pid,)).fetchall()
    cases = conn.execute(
        "SELECT id, input_text, reference_text, source, used_in, created_at FROM cases"
        " WHERE prompt_id=? ORDER BY id DESC", (pid,)).fetchall()
    runs = conn.execute(
        "SELECT id, kind, state, base_version_id, candidate_version_id, cap_type, cap_value,"
        " spent_requests, error, verdict, created_at, updated_at FROM runs"
        " WHERE prompt_id=? ORDER BY id DESC", (pid,)).fetchall()
    conn.close()
    return {"ok": True,
            "prompt": {"id": r["id"], "name": r["name"], "note": r["note"],
                       "official_version_id": r["official_version_id"],
                       "trial_version_id": r["trial_version_id"],
                       "use_pref": r["use_pref"]},
            "versions": [dict(v) for v in versions],
            "cases": [dict(c) for c in cases],
            "runs": [dict(x) for x in runs]}


@route("GET", r"/api/prompts/(\d+)/versions/(\d+)")
def api_get_version(handler, m):
    pid, vid = int(m.group(1)), int(m.group(2))
    conn = db()
    v = version_row(conn, vid, pid)
    conn.close()
    return {"ok": True, "version": dict(v)}


@route("POST", r"/api/prompts/(\d+)/versions")
def api_add_version(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    content = str(b.get("content") or "").strip()
    if not content:
        raise ApiError("版本内容不能为空。")
    conn = db()
    prompt_row(conn, pid)
    vid = add_version(conn, pid, content, str(b.get("label") or ""), "manual",
                      str(b.get("note") or ""))
    conn.commit()
    conn.close()
    return {"ok": True, "version_id": vid}


@route("POST", r"/api/prompts/(\d+)/cases")
def api_add_cases(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    items = b.get("cases") or []
    if not isinstance(items, list) or not items:
        raise ApiError("没有可保存的例子。")
    conn = db()
    prompt_row(conn, pid)
    saved, skipped = 0, 0
    for it in items:
        text = str(it.get("input") or "").strip()
        if not text:
            skipped += 1
            continue
        if len(text) > 20000:
            conn.close()
            raise ApiError("有一条例子超过 2 万字，先拆小或精简再贴。")
        conn.execute(
            "INSERT INTO cases(prompt_id, input_text, reference_text, source, created_at)"
            " VALUES(?,?,?,?,?)",
            (pid, text, str(it.get("reference") or ""), str(it.get("source") or "pasted"), now()))
        saved += 1
    conn.commit()
    conn.close()
    msg = "已保存 %d 条例子。" % saved
    if skipped:
        msg += " 有 %d 条是空的，没保存。" % skipped
    return {"ok": True, "saved": saved, "skipped": skipped, "message": msg}


@route("POST", r"/api/prompts/(\d+)/adopt")
def api_adopt(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    vid = b.get("version_id")
    force = bool(b.get("force"))
    conn = db()
    prompt_row(conn, pid)
    v = version_row(conn, vid, pid)
    verified = False
    if b.get("run_id"):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (b["run_id"],)).fetchone()
        if run and run["kind"] == "validate" and run["verdict"]:
            vd = json.loads(run["verdict"])
            verified = vd.get("can_adopt")
    if not verified and not force:
        conn.close()
        return {"ok": False, "need_force": True,
                "message": "这个版本还没有通过「考个试」。没考过试就转正，等于没验证就上岗，确定要这样吗？"}
    conn.execute("UPDATE prompts SET official_version_id=? WHERE id=?", (vid, pid))
    conn.execute("UPDATE versions SET label='正式' WHERE prompt_id=? AND label='正式'", (pid,))
    conn.execute("UPDATE versions SET label='正式' WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "已把 v%s 设为正式版。随时可以回滚。" % v["version_no"]}


@route("POST", r"/api/prompts/(\d+)/set_trial")
def api_set_trial(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    conn = db()
    prompt_row(conn, pid)
    v = version_row(conn, b.get("version_id"), pid)
    conn.execute("UPDATE prompts SET trial_version_id=? WHERE id=?", (v["id"], pid))
    conn.execute("UPDATE versions SET label='试用' WHERE prompt_id=? AND label='试用'", (pid,))
    conn.execute("UPDATE versions SET label='试用' WHERE id=?", (v["id"],))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "已把 v%s 设为试用版（未考试）。" % v["version_no"]}


@route("POST", r"/api/prompts/(\d+)/use_pref")
def api_use_pref(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    pref = b.get("pref")
    if pref not in ("auto", "official", "trial"):
        raise ApiError("取值不对。")
    conn = db()
    prompt_row(conn, pid)
    conn.execute("UPDATE prompts SET use_pref=? WHERE id=?", (pref, pid))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---- 运行 ----

@route("POST", r"/api/runs")
def api_create_run(handler, m):
    b = jbody(handler)
    pid = b.get("prompt_id")
    kind = b.get("kind")
    if kind not in ("explore", "validate"):
        raise ApiError("运行类型不对。")
    conn = db()
    prompt_row(conn, pid)
    s = get_settings(conn)
    if not api_ready(s):
        conn.close()
        raise ApiError("还没配置 AI 接口。请先到「设置」里填好接口地址、密钥和模型名。")
    base_vid = b.get("base_version_id")
    version_row(conn, base_vid, pid)
    cand_vid = b.get("candidate_version_id")
    if kind == "validate":
        if not cand_vid:
            conn.close()
            raise ApiError("考试需要指定要检验的试用版。")
        version_row(conn, cand_vid, pid)
    case_ids = b.get("case_ids") or []
    if kind == "explore" and not (2 <= len(case_ids) <= 8):
        conn.close()
        raise ApiError("探索比较贴 2—8 个例子就行，太多会白花钱。")
    if kind == "validate" and len(case_ids) < 5:
        conn.close()
        raise ApiError("考试至少需要 5 份没用过的新例子（建议 20 份以上）。")
    ok_cases = []
    ok_case_ids = []
    for cid in case_ids:
        c = conn.execute("SELECT * FROM cases WHERE id=? AND prompt_id=?",
                         (cid, pid)).fetchone()
        if not c:
            conn.close()
            raise ApiError("有例子不存在，请刷新后重试。")
        ok_cases.append(c)
        ok_case_ids.append(c["id"])
    if kind == "validate":
        for c in ok_cases:
            used = json.loads(c["used_in"] or "[]")
            for r2 in conn.execute(
                    "SELECT id FROM runs WHERE kind='validate' AND prompt_id=?",
                    (pid,)).fetchall():
                if r2["id"] in used:
                    conn.close()
                    raise ApiError(
                        "有例子已经参加过之前的考试。考过的例子再用就不算新证据了，请换没考过的新例子。")
    cap_type = str(b.get("cap_type") or s["cap_type"])
    if cap_type == "money":
        pi, po = unit_price(s)
        if pi is None and po is None:
            cap_type = "requests"
    try:
        cap_value = float(b.get("cap_value") if b.get("cap_value") is not None
                          else s["cap_value"])
    except (TypeError, ValueError):
        cap_value = 40.0
    cur = conn.execute(
        "INSERT INTO runs(prompt_id, kind, base_version_id, candidate_version_id, state,"
        " cap_type, cap_value, case_ids, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, kind, base_vid, cand_vid, "ready", cap_type, cap_value,
         json.dumps(ok_case_ids), now(), now()))
    run_id = cur.lastrowid
    if kind == "validate":
        for c in ok_cases:
            used = json.loads(c["used_in"] or "[]")
            used.append(run_id)
            conn.execute("UPDATE cases SET used_in=? WHERE id=?", (json.dumps(used), c["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "run_id": run_id}


@route("GET", r"/api/runs/(\d+)")
def api_get_run(handler, m):
    run_id = int(m.group(1))
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    total = len(json.loads(run["case_ids"] or "[]"))
    base_done = conn.execute(
        "SELECT COUNT(*) c FROM outputs WHERE run_id=? AND version_id=?",
        (run_id, run["base_version_id"])).fetchone()["c"]
    cand_done = None
    if run["candidate_version_id"]:
        cand_done = conn.execute(
            "SELECT COUNT(*) c FROM outputs WHERE run_id=? AND version_id=?",
            (run_id, run["candidate_version_id"])).fetchone()["c"]
    pairs_judged = conn.execute(
        "SELECT COUNT(*) c FROM pairs WHERE run_id=? AND rating_a IS NOT NULL",
        (run_id,)).fetchone()["c"]
    pairs_total = conn.execute(
        "SELECT COUNT(*) c FROM pairs WHERE run_id=?", (run_id,)).fetchone()["c"]
    conn.close()
    return {"ok": True, "run": dict(run), "cases_total": total,
            "base_done": base_done, "cand_done": cand_done,
            "pairs_total": pairs_total, "pairs_judged": pairs_judged}


@route("POST", r"/api/runs/(\d+)/step/base")
@route("POST", r"/api/runs/(\d+)/step/candidate")
def api_run_step(handler, m):
    run_id = int(m.group(1))
    is_cand = handler.path.endswith("/step/candidate")
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    if run["state"] in ("generating_base", "generating_candidate"):
        conn.close()
        raise ApiError("这一步已经在进行中，等它跑完就行。", 409)
    if run["state"] in ("comparing", "done"):
        conn.close()
        raise ApiError("这轮已经到比较环节了，不用重新生成。", 409)
    if is_cand:
        if not run["candidate_version_id"]:
            conn.close()
            raise ApiError("还没有要比较的新版本。")
        allowed = {"ready", "rating_base", "candidate_ready", "paused_cap", "failed", "stopped"}
        if run["kind"] == "explore" and run["state"] not in allowed:
            conn.close()
            raise ApiError("当前状态不能生成新版（%s）。" % run["state"])
        conn.execute("UPDATE runs SET state='generating_candidate', error=NULL, updated_at=? WHERE id=?",
                     (now(), run_id))
        vid = run["candidate_version_id"]
    else:
        conn.execute("UPDATE runs SET state='generating_base', error=NULL, updated_at=? WHERE id=?",
                     (now(), run_id))
        vid = run["base_version_id"]
    conn.commit()
    conn.close()
    threading.Thread(target=gen_worker, args=(run_id, vid), daemon=True).start()
    return {"ok": True}


@route("POST", r"/api/runs/(\d+)/cap")
def api_run_cap(handler, m):
    run_id = int(m.group(1))
    b = jbody(handler)
    try:
        cap_value = float(b.get("cap_value"))
    except (TypeError, ValueError):
        raise ApiError("上限要填一个数字。")
    if cap_value <= 0 or cap_value > 100000:
        raise ApiError("上限要在 0 到 100000 之间。")
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    old = float(run["cap_value"] or 0)
    if cap_value <= old:
        conn.close()
        raise ApiError("新上限（%g）不比原来（%g）高，没有意义。" % (cap_value, old))
    conn.execute("UPDATE runs SET cap_value=?, updated_at=? WHERE id=?",
                 (cap_value, now(), run_id))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "这轮的上限已从 %g 提高到 %g。点「继续」就能接着跑。"
            % (old, cap_value)}


@route("POST", r"/api/runs/(\d+)/stop")
def api_run_stop(handler, m):
    run_id = int(m.group(1))
    conn = db()
    conn.execute("UPDATE runs SET state='stopping', updated_at=? WHERE id=? AND state LIKE 'generating%'",
                 (now(), run_id))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "正在停止。已经发出的调用会算进账，不会再有新调用。"}


@route("GET", r"/api/runs/(\d+)/outputs")
def api_run_outputs(handler, m):
    run_id = int(m.group(1))
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    rows = conn.execute(
        "SELECT o.id, o.case_id, o.content, o.status, o.error, o.usable, o.problem_note,"
        " c.input_text, c.reference_text FROM outputs o JOIN cases c ON c.id=o.case_id"
        " WHERE o.run_id=? AND o.version_id=? ORDER BY c.id",
        (run_id, run["base_version_id"])).fetchall()
    conn.close()
    return {"ok": True, "outputs": [dict(r) for r in rows]}


@route("POST", r"/api/runs/(\d+)/ratings")
def api_run_ratings(handler, m):
    run_id = int(m.group(1))
    b = jbody(handler)
    ratings = b.get("ratings") or []
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    n = 0
    for r in ratings:
        if not valid_usable(r.get("usable")):
            continue
        conn.execute("UPDATE outputs SET usable=?, problem_note=? WHERE id=? AND run_id=?",
                     (r["usable"], str(r.get("problem_note") or ""), r.get("output_id"), run_id))
        n += 1
    conn.commit()
    conn.close()
    return {"ok": True, "saved": n, "message": "已保存 %d 条评价。" % n}


@route("POST", r"/api/runs/(\d+)/improve")
def api_run_improve(handler, m):
    run_id = int(m.group(1))
    b = jbody(handler)
    problem = str(b.get("problem_summary") or "").strip()
    if not problem:
        raise ApiError("先用一两句话写下你最想解决的问题。")
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    if not run["candidate_version_id"] is None and run["kind"] == "validate":
        conn.close()
        raise ApiError("考试环节不需要再改写。")
    settings = get_settings(conn)
    if not api_ready(settings):
        conn.close()
        raise ApiError("还没配置 AI 接口，请先到设置里填好。")
    hit, reason = cap_reached(conn, run, settings)
    if hit:
        conn.close()
        raise ApiError(reason)
    base_version = conn.execute("SELECT * FROM versions WHERE id=?",
                                (run["base_version_id"],)).fetchone()
    outs = conn.execute(
        "SELECT o.usable, o.problem_note, o.content, c.input_text FROM outputs o"
        " JOIN cases c ON c.id=o.case_id WHERE o.run_id=? AND o.version_id=? ORDER BY c.id",
        (run_id, run["base_version_id"])).fetchall()
    conn.close()
    examples_txt = []
    for i, o in enumerate(outs, 1):
        verdict_cn = {"usable": "能直接用", "minor": "改改能用", "unusable": "不能用",
                      "unknown": "说不上来", None: "没评"}.get(o["usable"], "没评")
        block = "例子%d：\n输入：%s\n现在的输出：%s\n用户评价：%s" % (
            i, o["input_text"][:1500], (o["content"] or "")[:1500], verdict_cn)
        if o["problem_note"]:
            block += "\n用户指出的问题：" + o["problem_note"][:500]
        examples_txt.append(block)
    sys_prompt = (
        "你是一个提示词优化助手。用户有一个在用的提示词，在一些真实例子上表现不理想。\n"
        "请根据用户指出的问题，改写这个提示词，让它避免这些问题，同时保留原来做得好的地方。\n"
        "要求：\n"
        "1. 只改提示词本身，不要输出任何例子或解释性文字之外的内容。\n"
        "2. 改动要针对用户指出的问题，不要大幅重写无关部分。\n"
        "3. 按下面格式输出：\n"
        "【改动说明】一句话说明改了什么\n"
        "【新提示词】\n"
        "（紧接着输出完整的新提示词，可以直接拿来使用）")
    user_prompt = (
        "【现在的提示词】\n%s\n\n【真实例子与评价】\n%s\n\n【用户最想解决的问题】\n%s"
        % (base_version["content"], "\n\n".join(examples_txt), problem))
    result = call_llm(settings, [{"role": "system", "content": sys_prompt},
                                 {"role": "user", "content": user_prompt}],
                      max_tokens=4000, temperature=0.4)
    conn = db()
    est = est_cost_of(settings, result["tokens_in"], result["tokens_out"]) if result["ok"] else None
    ledger_add(conn, run_id, "优化·生成改进版", settings["model"], result["tokens_in"],
               result["tokens_out"], est, "ok" if result["ok"] else "error",
               (result.get("error") or "")[:200])
    if not result["ok"]:
        conn.commit()
        conn.close()
        raise ApiError(result["error"], 502)
    text = result["content"]
    change_note, new_content = "", ""
    m1 = re.search(r"【改动说明】\s*(.+)", text)
    m2 = re.search(r"【新提示词】\s*(.+)\s*$", text, re.S)
    if m1:
        change_note = m1.group(1).strip()
    if m2:
        new_content = m2.group(1).strip()
    else:
        new_content = text.strip()
    if not new_content:
        conn.close()
        raise ApiError("模型没有给出新提示词，可以重试一次。")
    pid = run["prompt_id"]
    if run["candidate_version_id"]:
        vid = run["candidate_version_id"]
        conn.execute("UPDATE versions SET content=?, note=? WHERE id=?",
                     (new_content, change_note, vid))
    else:
        vid = add_version(conn, pid, new_content, "试用", "improve", change_note,
                          parent=run["base_version_id"])
        conn.execute("UPDATE runs SET candidate_version_id=? WHERE id=?", (vid, run_id))
    conn.execute("UPDATE runs SET problem_summary=?, state='candidate_ready', updated_at=? WHERE id=?",
                 (problem, now(), run_id))
    conn.commit()
    conn.close()
    return {"ok": True, "version_id": vid, "change_note": change_note,
            "content": new_content, "spent": {"requests": 1}}


@route("POST", r"/api/runs/(\d+)/candidate")
def api_run_candidate(handler, m):
    run_id = int(m.group(1))
    b = jbody(handler)
    content = str(b.get("content") or "").strip()
    if not content:
        raise ApiError("新版本内容不能为空。")
    conn = db()
    run = get_run(conn, run_id)
    if not run or not run["candidate_version_id"]:
        conn.close()
        raise ApiError("这轮还没有新版本可改。", 404)
    conn.execute("UPDATE versions SET content=? WHERE id=?",
                 (content, run["candidate_version_id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


@route("GET", r"/api/runs/(\d+)/pairs")
def api_run_pairs(handler, m):
    run_id = int(m.group(1))
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    rows = conn.execute(
        "SELECT p.id, p.case_id, p.rating_a, p.rating_b, p.preference, p.a_is_candidate,"
        " oa.content ca, ob.content cb, c.input_text, c.reference_text"
        " FROM pairs p"
        " JOIN outputs oa ON oa.id=p.output_a_id JOIN outputs ob ON ob.id=p.output_b_id"
        " JOIN cases c ON c.id=p.case_id WHERE p.run_id=? ORDER BY p.id", (run_id,)).fetchall()
    out = []
    for r in rows:
        judged = bool(r["rating_a"] and r["rating_b"])
        out.append({
            "id": r["id"], "case_id": r["case_id"],
            "input": r["input_text"], "reference": r["reference_text"],
            "a": r["ca"], "b": r["cb"],
            "rating_a": r["rating_a"], "rating_b": r["rating_b"],
            "preference": r["preference"], "judged": judged,
            "a_is_candidate": bool(r["a_is_candidate"]) if judged else None,
        })
    conn.close()
    return {"ok": True, "pairs": out}


@route("POST", r"/api/pairs/(\d+)")
def api_rate_pair(handler, m):
    pid = int(m.group(1))
    b = jbody(handler)
    ra, rb, pref = b.get("rating_a"), b.get("rating_b"), b.get("preference")
    if not (valid_usable(ra) and valid_usable(rb) and valid_pref(pref)):
        raise ApiError("评价取值不对。两边都要选，可以说「说不上来」。")
    conn = db()
    row = conn.execute("SELECT run_id, output_a_id, output_b_id, a_is_candidate FROM pairs WHERE id=?",
                       (pid,)).fetchone()
    if not row:
        conn.close()
        raise ApiError("找不到这份比较。", 404)
    conn.execute("UPDATE pairs SET rating_a=?, rating_b=?, preference=?, judged_at=? WHERE id=?",
                 (ra, rb, pref, now(), pid))
    # 每边的"能不能用"同时落到 outputs 上，供结论统计使用
    conn.execute("UPDATE outputs SET usable=? WHERE id=?", (ra, row["output_a_id"]))
    conn.execute("UPDATE outputs SET usable=? WHERE id=?", (rb, row["output_b_id"]))
    run_id = row["run_id"]
    left = conn.execute("SELECT COUNT(*) c FROM pairs WHERE run_id=? AND rating_a IS NULL",
                        (run_id,)).fetchone()["c"]
    if left == 0:
        run = get_run(conn, run_id)
        verdict = compute_verdict(conn, run)
        conn.execute("UPDATE runs SET state='done', verdict=?, updated_at=? WHERE id=?",
                     (json.dumps(verdict, ensure_ascii=False), now(), run_id))
    conn.commit()
    conn.close()
    return {"ok": True, "revealed": bool(row["a_is_candidate"])}


@route("GET", r"/api/runs/(\d+)/verdict")
def api_run_verdict(handler, m):
    run_id = int(m.group(1))
    conn = db()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        raise ApiError("找不到这轮运行。", 404)
    if not run["verdict"]:
        conn.close()
        return {"ok": True, "verdict": None}
    conn.close()
    return {"ok": True, "verdict": json.loads(run["verdict"])}


@route("POST", r"/api/runs/(\d+)/save_trial")
def api_save_trial(handler, m):
    run_id = int(m.group(1))
    conn = db()
    run = get_run(conn, run_id)
    if not run or not run["candidate_version_id"]:
        conn.close()
        raise ApiError("这轮没有可保存的新版本。", 404)
    vid = run["candidate_version_id"]
    conn.execute("UPDATE prompts SET trial_version_id=? WHERE id=?", (vid, run["prompt_id"]))
    conn.execute("UPDATE versions SET label='试用' WHERE prompt_id=? AND label='试用'",
                 (run["prompt_id"],))
    conn.execute("UPDATE versions SET label='试用' WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "已存为试用版（未考试）。平时可以先在「用一用」里试它，攒够例子再考个试。"}


@route("GET", r"/api/exams")
def api_exams(handler, m):
    conn = db()
    rows = conn.execute(
        "SELECT p.id pid, p.name, p.trial_version_id, p.official_version_id,"
        " tv.version_no trial_v, ov.version_no official_v"
        " FROM prompts p"
        " LEFT JOIN versions tv ON tv.id=p.trial_version_id"
        " LEFT JOIN versions ov ON ov.id=p.official_version_id"
        " WHERE p.archived=0 AND p.trial_version_id IS NOT NULL ORDER BY p.id DESC").fetchall()
    out = []
    for r in rows:
        validated = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE kind='validate' AND prompt_id=? AND state='done'",
            (r["pid"],)).fetchone()["c"]
        fresh = 0
        if r["trial_version_id"]:
            validate_ids = {x["id"] for x in conn.execute(
                "SELECT id FROM runs WHERE kind='validate' AND prompt_id=?",
                (r["pid"],)).fetchall()}
            for u in conn.execute("SELECT used_in FROM cases WHERE prompt_id=?",
                                  (r["pid"],)).fetchall():
                used = json.loads(u["used_in"] or "[]")
                if not any(x in validate_ids for x in used):
                    fresh += 1
        last = conn.execute(
            "SELECT id, verdict FROM runs WHERE kind='validate' AND prompt_id=? AND state='done'"
            " ORDER BY id DESC LIMIT 1", (r["pid"],)).fetchone()
        out.append({"prompt_id": r["pid"], "name": r["name"],
                    "trial_v": r["trial_v"], "official_v": r["official_v"],
                    "validated": validated, "fresh_cases": fresh,
                    "last_run_id": last["id"] if last else None,
                    "last_verdict": json.loads(last["verdict"]) if last else None})
    conn.close()
    return {"ok": True, "exams": out}

# ---- 日常使用 ----

def pick_use_version(conn, prompt):
    if prompt["use_pref"] == "trial" and prompt["trial_version_id"]:
        return prompt["trial_version_id"], "试用版（未考试）"
    if prompt["use_pref"] == "official" and prompt["official_version_id"]:
        return prompt["official_version_id"], "正式版"
    if prompt["official_version_id"]:
        return prompt["official_version_id"], "正式版"
    if prompt["trial_version_id"]:
        return prompt["trial_version_id"], "试用版（未考试）"
    row = conn.execute("SELECT id, version_no FROM versions WHERE prompt_id=?"
                       " ORDER BY version_no DESC LIMIT 1", (prompt["id"],)).fetchone()
    return (row["id"], "v%d" % row["version_no"]) if row else (None, "")


@route("GET", r"/api/use/options")
def api_use_options(handler, m):
    conn = db()
    rows = conn.execute("SELECT id, name FROM prompts WHERE archived=0 ORDER BY id").fetchall()
    out = []
    for r in rows:
        p = conn.execute("SELECT * FROM prompts WHERE id=?", (r["id"],)).fetchone()
        vid, vlabel = pick_use_version(conn, p)
        out.append({"id": r["id"], "name": r["name"], "version_id": vid, "version_label": vlabel})
    conn.close()
    return {"ok": True, "options": out}


@route("POST", r"/api/use")
def api_use(handler, m):
    b = jbody(handler)
    pid = b.get("prompt_id")
    text = str(b.get("input") or "").strip()
    if not text:
        raise ApiError("先把要处理的输入贴进来。")
    if len(text) > 20000:
        raise ApiError("输入超过 2 万字，先精简一下。")
    conn = db()
    prompt_row(conn, pid)
    s = get_settings(conn)
    if not api_ready(s):
        conn.close()
        raise ApiError("还没配置 AI 接口，请先到设置里填好。")
    vid, vlabel = pick_use_version(conn, conn.execute(
        "SELECT * FROM prompts WHERE id=?", (pid,)).fetchone())
    if not vid:
        conn.close()
        raise ApiError("这条提示词还没有任何版本。")
    version = version_row(conn, vid, pid)
    conn.close()
    result = call_llm(s, [{"role": "user", "content": render_prompt(version["content"], text)}],
                      max_tokens=2048, temperature=0.5)
    conn = db()
    est = est_cost_of(s, result["tokens_in"], result["tokens_out"]) if result["ok"] else None
    cur = conn.execute(
        "INSERT INTO usage_log(prompt_id, version_id, input_text, output_text, status, error,"
        " tokens_in, tokens_out, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, vid, text, result.get("content"), "ok" if result["ok"] else "error",
         result.get("error"), result["tokens_in"], result["tokens_out"], now()))
    uid = cur.lastrowid
    ledger_add(conn, None, "日常使用", s["model"], result["tokens_in"], result["tokens_out"],
               est, "ok" if result["ok"] else "error", (result.get("error") or "")[:200])
    conn.commit()
    conn.close()
    if not result["ok"]:
        return {"ok": False, "use_id": uid, "message": result["error"]}
    return {"ok": True, "use_id": uid, "output": result["content"],
            "version_label": vlabel,
            "cost_note": ("本次约消耗 %d+%d tokens%s"
                          % (result["tokens_in"], result["tokens_out"],
                             ("，约 %.4f 元" % est) if est is not None else ""))}


@route("POST", r"/api/use/(\d+)/feedback")
def api_use_feedback(handler, m):
    uid = int(m.group(1))
    b = jbody(handler)
    fb = b.get("result")
    if fb not in ("as_is", "edited", "bad"):
        raise ApiError("反馈取值不对。")
    edited = str(b.get("edited_text") or "").strip()
    if fb == "edited" and not edited:
        raise ApiError("选了「改了改」，把你改后的文本贴进来，方便下次对比。")
    conn = db()
    row = conn.execute("SELECT * FROM usage_log WHERE id=?", (uid,)).fetchone()
    if not row:
        conn.close()
        raise ApiError("找不到这条使用记录。", 404)
    case_id = None
    if b.get("add_to_pool", True):
        cur = conn.execute(
            "INSERT INTO cases(prompt_id, input_text, reference_text, source, created_at)"
            " VALUES(?,?,?,?,?)",
            (row["prompt_id"], row["input_text"], "", "usage", now()))
        case_id = cur.lastrowid
    conn.execute("UPDATE usage_log SET feedback=?, edited_text=?, case_id=? WHERE id=?",
                 (fb, edited if fb == "edited" else None, case_id, uid))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "记下了。这条输入也存进了例子池，以后考个试可能会用到。"}


@route("GET", r"/api/use/(\d+)")
def api_use_get(handler, m):
    uid = int(m.group(1))
    conn = db()
    row = conn.execute("SELECT * FROM usage_log WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        raise ApiError("找不到这条使用记录。", 404)
    return {"ok": True, "usage": dict(row)}

# ---- 账本 ----

@route("GET", r"/api/ledger")
def api_ledger(handler, m):
    conn = db()
    rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT 200").fetchall()
    agg = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) ti,"
        " COALESCE(SUM(completion_tokens),0) to_, COALESCE(SUM(est_cost),0) cost"
        " FROM ledger WHERE status='ok'").fetchone()
    month = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(est_cost),0) cost FROM ledger"
        " WHERE status='ok' AND ts LIKE ?", (now()[:7] + "%",)).fetchone()
    conn.close()
    return {"ok": True, "entries": [dict(r) for r in rows],
            "total": {"requests": agg["n"], "tokens_in": agg["ti"],
                      "tokens_out": agg["to_"],
                      "cost_est": agg["cost"] if agg["cost"] else None},
            "month": {"requests": month["n"],
                      "cost_est": month["cost"] if month["cost"] else None}}

# ---------------------------------------------------------------- Handler

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PromptLab/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _static(self, path):
        if path in ("/", "/index.html"):
            fp = os.path.join(STATIC_DIR, "index.html")
        else:
            name = os.path.basename(path)
            fp = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(fp):
                self._json({"ok": False, "message": "页面不存在"}, 404)
                return
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self._json({"ok": False, "message": "文件读取失败"}, 500)
            return
        ext = os.path.splitext(fp)[1]
        self._send(200, MIME.get(ext, "application/octet-stream"), body)

    def _dispatch(self, method):
        path = self.path.split("?")[0]
        if method == "GET" and not path.startswith("/api/"):
            if path in ("/", "/index.html", "/app.js", "/style.css", "/favicon.ico"):
                return self._static(path)
            return self._json({"ok": False, "message": "接口不存在"}, 404)
        for m_, rx, fn in API_ROUTES:
            if m_ != method:
                continue
            match = rx.match(path)
            if match:
                try:
                    result = fn(self, match)
                    self._json(result)
                except ApiError as e:
                    self._json({"ok": False, "message": e.msg}, e.code)
                except Exception as e:  # noqa: BLE001 兜底，避免线程静默
                    self._json({"ok": False,
                                "message": "程序内部出错了：%s" % e}, 500)
                return
        self._json({"ok": False, "message": "接口不存在"}, 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

# ---------------------------------------------------------------- 入口

def find_free_port(start=8765):
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def doctor():
    print("自检开始……")
    ok = True
    try:
        v = sys.version_info
        print("Python 版本：%d.%d.%d" % (v.major, v.minor, v.micro))
        if v.major < 3 or (v.major == 3 and v.minor < 8):
            print("!! 需要 Python 3.8 以上")
            ok = False
    except Exception as e:  # noqa: BLE001
        print("!! Python 版本读取失败：%s" % e)
        ok = False
    try:
        init_db()
        conn = db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        print("数据库：正常（%s）" % DB_PATH)
    except Exception as e:  # noqa: BLE001
        print("!! 数据库异常：%s" % e)
        ok = False
    try:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            print("本地端口：可用")
    except OSError as e:
        print("!! 本地端口绑定失败：%s" % e)
        ok = False
    print("自检%s" % ("通过。" if ok else "发现问题，见上面。"))
    return 0 if ok else 1


def main():
    if "--check" in sys.argv:
        sys.exit(doctor())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    init_db()
    port_arg = None
    if "--port" in sys.argv:
        port_arg = int(sys.argv[sys.argv.index("--port") + 1])
    port = port_arg or find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d" % port
    print("=" * 46)
    print("  提示词优化实验室 · 单机版 v0.1")
    print("  已在本机启动：%s" % url)
    print("  数据保存在本机 data/lab.db，仅本机可访问。")
    print("  关掉这个窗口即退出程序。")
    print("=" * 46)
    threading.Timer(0.8, webbrowser.open, args=(url,)).start() if "--no-browser" not in sys.argv else None
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()

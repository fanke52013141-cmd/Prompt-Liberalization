/* 提示词优化实验室 · 单机版 前端逻辑（无框架，无构建） */
"use strict";

var $app = document.getElementById("app");
var USABLE = [
  ["usable", "能直接用"],
  ["minor", "改改能用"],
  ["unusable", "不能用"],
  ["unknown", "说不上来"]
];
var PREF = [
  ["a", "甲更好"],
  ["b", "乙更好"],
  ["tie", "差不多"],
  ["unknown", "说不清"]
];
var RUN_STATE_LABEL = {
  ready: "还没开始",
  generating_base: "正在生成原版的输出…",
  rating_base: "等你评价原版的输出",
  candidate_ready: "新版已生成，等你确认",
  generating_candidate: "正在生成新版的输出…",
  comparing: "等你逐份盲评比较",
  done: "已完成",
  paused_cap: "到了上限，已暂停",
  stopping: "正在停止…",
  stopped: "已停止",
  failed: "出错了"
};
var ORIGIN_LABEL = { import: "最初贴入", improve: "优化生成", manual: "手动新建" };

/* ---------- 基础工具 ---------- */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path, opts) {
  opts = opts || {};
  var init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.method = opts.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  var res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new Error("连不上本机服务。请确认「启动」的黑窗口还开着。");
  }
  var data = null;
  try { data = await res.json(); } catch (e) { /* 忽略 */ }
  if (!res.ok || (data && data.ok === false)) {
    throw new Error((data && data.message) || ("请求失败（" + res.status + "）"));
  }
  return data;
}

var toastTimer = null;
function toast(msg) {
  var t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.classList.add("hidden"); }, 3600);
}

function setNav(name) {
  var links = document.querySelectorAll("#nav a");
  for (var i = 0; i < links.length; i++) {
    links[i].classList.toggle("active", links[i].getAttribute("data-nav") === name);
  }
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { toast("已复制。"); },
      function () { toast("复制失败，请手动选择复制。"); });
  } else {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("已复制。"); } catch (e) { toast("复制失败，请手动复制。"); }
    document.body.removeChild(ta);
  }
}

function fmtCost(v) {
  if (v == null) return "—";
  if (v < 0.01) return "不足 1 分钱";
  return "约 " + (Math.round(v * 100) / 100) + " 元";
}

/* ---------- 路由 ---------- */

var pollTimer = null;
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function route() {
  stopPoll();
  var hash = location.hash || "#/";
  var seg = hash.slice(2).split("/"); // 去掉 "#/"
  setNav(seg[0] || "home");
  try {
    if (hash === "#/" || hash === "#") return await viewHome();
    if (seg[0] === "new") return await viewNew();
    if (seg[0] === "prompt" && seg[1]) return await viewPrompt(parseInt(seg[1], 10));
    if (seg[0] === "run" && seg[1]) return await viewRun(parseInt(seg[1], 10));
    if (seg[0] === "use") return await viewUse();
    if (seg[0] === "exams") return await viewExams();
    if (seg[0] === "ledger") return await viewLedger();
    if (seg[0] === "settings") return await viewSettings();
    return await viewHome();
  } catch (e) {
    $app.innerHTML = '<div class="banner warn">' + esc(e.message) +
      ' <a href="#/">回首页</a></div>';
  }
}
window.addEventListener("hashchange", route);

/* ---------- 首页 ---------- */

async function viewHome() {
  var state = await api("/api/state");
  var data = await api("/api/prompts");
  var html = "<h1>我的提示词</h1>";
  if (!state.configured) {
    html += '<div class="banner">第一次用？先去 <a href="#/settings">设置</a> 里填好 AI 接口地址、密钥和模型名（约 1 分钟），再回来点「新建优化」。</div>';
  }
  html += '<div class="btn-row" style="margin-bottom:16px"><a class="btn primary big" href="#/new">＋ 新建优化</a>' +
    '<span class="muted small">把你在用的提示词贴进来，贴几个真实例子，看它哪里不行，改好给你对比。</span></div>';
  if (!data.prompts.length) {
    html += '<div class="empty"><div class="big">这里还是空的</div>' +
      '点上面「新建优化」：贴一条你在用的提示词，再贴 2—3 个真实任务的输入，就能看到第一轮对比。</div>';
    $app.innerHTML = html;
    return;
  }
  for (var i = 0; i < data.prompts.length; i++) {
    var p = data.prompts[i];
    var tags = "";
    if (p.official_v != null) tags += '<span class="badge official">正式 v' + p.official_v + "</span>";
    if (p.trial_v != null) tags += '<span class="badge trial">试用 v' + p.trial_v + "（未考试）</span>";
    if (!tags) tags = '<span class="badge gray">还没有正式版或试用版</span>';
    html += '<div class="list-item"><div class="title">' + esc(p.name) + tags + "</div>" +
      '<div class="meta">例子 ' + p.cases + " 条 · 日常用过 " + p.usage + " 次</div>" +
      '<div class="btn-row"><a class="btn" href="#/prompt/' + p.id + '">打开</a>' +
      '<a class="btn" href="#/use">用一用</a></div></div>';
  }
  $app.innerHTML = html;
}

/* ---------- 新建优化 ---------- */

function exampleBlocks(n) {
  var html = "";
  for (var i = 0; i < n; i++) {
    html += '<label class="field"><span>例子 ' + (i + 1) + "（真实任务的完整输入）" +
      '<a href="javascript:void(0)" onclick="removeExample(this)" class="muted small" style="float:right">删除</a></span>' +
      '<textarea name="ex-in" placeholder="把一条真实任务的输入原样贴到这里"></textarea>' +
      '<div class="hint">可选：如果有参考答案或核对材料，贴到下面，将来判断时用得上。</div>' +
      '<textarea name="ex-ref" style="min-height:44px" placeholder="参考材料（可留空）"></textarea></label>';
  }
  return html;
}
window.removeExample = function (el) {
  var field = el.closest("label.field");
  if (field) field.remove();
};
window.addExample = function () {
  var box = document.getElementById("examples");
  var count = box.querySelectorAll("textarea[name=ex-in]").length;
  if (count >= 8) { toast("探索比较 2—8 个例子就够，多了白花钱。"); return; }
  box.insertAdjacentHTML("beforeend", exampleBlocks(1));
};

async function viewNew() {
  $app.innerHTML =
    "<h1>新建优化</h1>" +
    '<p class="sub">四步：贴提示词 → 贴几个真实例子 → 指出问题 → 比一比。全程只花几毛到几块钱（上限在设置里）。</p>' +
    '<div class="card">' +
    '<label class="field"><span>给它起个名字</span>' +
    '<input type="text" id="np-name" placeholder="给它起个好认的名字，随你取" maxlength="50"></label>' +
    '<label class="field"><span>你现在在用的提示词（原样贴进来，不用整理）</span>' +
    '<textarea id="np-content" style="min-height:160px" placeholder="把你反复在用的那条提示词，原样复制到这里。"></textarea>' +
    '<div class="hint">如果你的提示词里写了 {输入} 两个花括号占位，系统会把每个例子替换到那个位置；没写也没关系，例子会自动附在提示词后面。</div></label>' +
    '<div class="field-label">贴 2—8 个真实例子（每个例子就是一次真实任务的输入）</div>' +
    '<div id="examples">' + exampleBlocks(3) + "</div>" +
    '<div class="btn-row"><button class="btn" onclick="addExample()">＋ 再加一个例子</button></div>' +
    '<div class="btn-row" style="margin-top:20px">' +
    '<button class="btn primary big" onclick="submitNew()">创建并开始：先看看现在的水平</button>' +
    '<span class="muted small">下一步会先给每个例子生成一次输出，让你看清现状。</span></div>' +
    "</div>";
}
window.submitNew = async function () {
  var name = document.getElementById("np-name").value.trim() || "未命名提示词";
  var content = document.getElementById("np-content").value.trim();
  if (!content) { toast("先把你在用的提示词贴进来。"); return; }
  var ins = document.querySelectorAll("textarea[name=ex-in]");
  var refs = document.querySelectorAll("textarea[name=ex-ref]");
  var cases = [];
  for (var i = 0; i < ins.length; i++) {
    var t = ins[i].value.trim();
    if (t) cases.push({ input: t, reference: refs[i] ? refs[i].value.trim() : "" });
  }
  if (cases.length < 2) { toast("至少贴 2 个例子，才能看出问题是不是反复出现。"); return; }
  var p = await api("/api/prompts", { body: { name: name, content: content } });
  await api("/api/prompts/" + p.prompt_id + "/cases", { body: { cases: cases } });
  var detail = await api("/api/prompts/" + p.prompt_id);
  var ids = detail.cases.map(function (c) { return c.id; }).reverse().slice(0, cases.length);
  var r = await api("/api/runs", { body: {
    prompt_id: p.prompt_id, kind: "explore",
    base_version_id: p.version_id, case_ids: ids } });
  location.hash = "#/run/" + r.run_id;
};

/* ---------- 运行页（探索与考试共用） ---------- */

function progressBar(done, total) {
  var pct = total ? Math.round(done / total * 100) : 0;
  return '<div class="progress-bar"><div style="width:' + pct + '%"></div></div>' +
    '<div class="muted small">已完成 ' + done + " / " + total + "</div>";
}

async function viewRun(runId) {
  var data = await api("/api/runs/" + runId);
  renderRun(runId, data);
}

function renderRun(runId, data) {
  var run = data.run;
  var kindLabel = run.kind === "validate" ? "考个试" : "探索比较";
  var head = "<h1>" + kindLabel + " <span class=\"badge gray\">" +
    (RUN_STATE_LABEL[run.state] || run.state) + "</span></h1>" +
    '<p class="sub"><a href="#/prompt/' + run.prompt_id + '">← 回到这条提示词</a></p>';
  var body = "";
  var st = run.state;

  if (st === "ready") {
    var n = JSON.parse(run.case_ids || "[]").length;
    var est = run.kind === "validate" ? n * 2 : n;
    body = '<div class="card"><p>准备就绪。接下来会调用 AI 给每条例子生成输出' +
      (run.kind === "validate" ? "（原版和新版各一遍）" : "") +
      "，预计约 " + est + " 次调用，上限是 " +
      (run.cap_type === "money" ? ("约 " + run.cap_value + " 元") : (run.cap_value + " 次")) +
      "，到上限会自动停。</p>" +
      '<div class="btn-row"><button class="btn primary big" onclick="startBase(' + runId + ")'>" +
      (run.kind === "validate" ? "开始考试" : "开始生成") + "</button></div></div>";
  } else if (st === "generating_base" || st === "generating_candidate") {
    var done = st === "generating_base" ? data.base_done : (data.cand_done || 0);
    body = '<div class="card">' + progressBar(done, data.cases_total) +
      '<p class="muted small">关掉这个页面没关系，后台会继续；回来刷新就能看到。</p>' +
      '<div class="btn-row"><button class="btn" onclick="stopRun(' + runId + ')">停止新的调用</button>' +
      '<span class="muted small">已发出的调用会照常计费结算。</span></div></div>';
    pollTimer = setInterval(async function () {
      try {
        var d2 = await api("/api/runs/" + runId);
        if (d2.run.state !== st) { stopPoll(); renderRun(runId, d2); }
      } catch (e) { /* 下轮再试 */ }
    }, 2000);
  } else if (st === "rating_base" && run.kind === "explore") {
    pollTimer = null;
    return renderRating(runId);
  } else if (st === "candidate_ready") {
    return renderCandidate(runId);
  } else if (st === "comparing") {
    return renderCompare(runId);
  } else if (st === "done") {
    return renderVerdict(runId);
  } else if (st === "paused_cap" || st === "failed" || st === "stopped") {
    var capBtn = st === "paused_cap"
      ? '<button class="btn primary" onclick="raiseCapAndResume(' + runId + ')">提高上限并继续</button>'
      : "";
    body = '<div class="card"><p>' + esc(run.error || "已停止。") + "</p>" +
      '<div class="btn-row">' + capBtn +
      '<button class="btn" onclick="resumeRun(' + runId + ')">继续（上限没变）</button></div></div>';
  } else {
    body = '<div class="card"><p class="muted">' + (RUN_STATE_LABEL[st] || st) + "</p></div>";
  }
  $app.innerHTML = head + body;
}

window.startBase = async function (runId) {
  try {
    await api("/api/runs/" + runId + "/step/base", { body: {} });
    viewRun(runId);
  } catch (e) { toast(e.message); }
};
window.stopRun = async function (runId) {
  try { var r = await api("/api/runs/" + runId + "/stop", { body: {} }); toast(r.message); }
  catch (e) { toast(e.message); }
};
window.raiseCapAndResume = async function (runId) {
  var v = window.prompt("把这轮优化的上限提高到多少？（当前上到顶已自动暂停）", "60");
  if (v == null) return;
  var n = parseFloat(v);
  if (!n || n <= 0) { toast("要填一个大于 0 的数字。"); return; }
  try {
    var r = await api("/api/runs/" + runId + "/cap", { body: { cap_value: n } });
    toast(r.message);
    resumeRun(runId);
  } catch (e) { toast(e.message); }
};
window.resumeRun = async function (runId) {
  try {
    var d = await api("/api/runs/" + runId);
    var run = d.run;
    var baseNeed = d.base_done < d.cases_total;
    if (baseNeed) {
      await api("/api/runs/" + runId + "/step/base", { body: {} });
    } else if (run.candidate_version_id && (d.cand_done == null || d.cand_done < d.cases_total)) {
      await api("/api/runs/" + runId + "/step/candidate", { body: {} });
    } else if (run.state === "paused_cap" && run.kind === "explore" && !run.candidate_version_id) {
      toast("原版已生成完。等一下，页面会带你去评价和改写。");
      return;
    }
    viewRun(runId);
  } catch (e) { toast(e.message); }
};

/* ----- 评价原版输出 ----- */

async function renderRating(runId) {
  var d = await api("/api/runs/" + runId + "/outputs");
  var outs = d.outputs;
  var html = "<h1>先看看现在的水平</h1>" +
    '<p class="sub">下面是「你现在的提示词」给每个例子生成的输出。逐份判断：<b>能直接用吗？</b>如果不行，顺手写一句最要紧的问题。</p>';
  for (var i = 0; i < outs.length; i++) {
    var o = outs[i];
    html += '<div class="card" data-oid="' + o.id + '">' +
      '<details' + (o.status !== "ok" ? "" : "") + ' open><summary class="field-label">例子 ' + (i + 1) +
      "（点开/收起输入）</summary>" +
      '<div class="output-box" style="max-height:120px">' + esc(o.input_text) + "</div></details>";
    if (o.status !== "ok") {
      html += '<p class="small" style="color:var(--bad)">这条没生成出来：' + esc(o.error || "未知原因") + "。不影响其他条，先评其他的。</p></div>";
      continue;
    }
    html += '<div class="field-label mt">AI 的输出：</div><div class="output-box">' + esc(o.content) + "</div>" +
      '<div class="field-label mt">这份能直接用吗？</div><div class="opt-row" data-role="usable">';
    for (var j = 0; j < USABLE.length; j++) {
      html += '<button class="opt" onclick="pickOpt(this)" data-v="' + USABLE[j][0] + '">' + USABLE[j][1] + "</button>";
    }
    html += "</div>" +
      '<label class="field mt"><span class="muted small">最要紧的问题（可留空，一句话就够）</span>' +
      '<input type="text" data-role="note" placeholder="例如：把学生写对的答案判成了错的"></label>' +
      "</div>";
  }
  html += '<div class="card"><div class="btn-row"><button class="btn primary big" onclick="submitRatings(' + runId + ')">评完了，下一步：改一版试试</button></div>' +
    '<div class="hint mt">每份都要选一个（可以选「说不上来」）。下一步只需 1 次调用，让 AI 针对你指出的问题改出一版新提示词，改完你还能手动再改。</div></div>';
  $app.innerHTML = html;
}
window.pickOpt = function (btn) {
  var row = btn.closest(".opt-row");
  var btns = row.querySelectorAll(".opt");
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove("sel");
  btn.classList.add("sel");
};
window.submitRatings = async function (runId) {
  var cards = document.querySelectorAll(".card[data-oid]");
  var ratings = [];
  for (var i = 0; i < cards.length; i++) {
    var c = cards[i];
    var sel = c.querySelector(".opt-row .opt.sel");
    if (!sel) { toast("还有一份没选「能不能用」（可以选「说不上来」）。"); return; }
    var note = c.querySelector("input[data-role=note]");
    ratings.push({ output_id: parseInt(c.getAttribute("data-oid"), 10),
      usable: sel.getAttribute("data-v"),
      problem_note: note ? note.value.trim() : "" });
  }
  await api("/api/runs/" + runId + "/ratings", { body: { ratings: ratings } });
  renderImprove(runId);
};

/* ----- 问题汇总与生成改进版 ----- */

async function renderImprove(runId) {
  var d = await api("/api/runs/" + runId + "/outputs");
  var notes = [];
  for (var i = 0; i < d.outputs.length; i++) {
    var o = d.outputs[i];
    if (o.problem_note) notes.push(o.problem_note);
  }
  var prefill = Array.from(new Set(notes)).join("；");
  $app.innerHTML = "<h1>这一轮最想解决什么？</h1>" +
    '<p class="sub">把你指出的摆放在一起，你来概括一两句。AI 会据此改出一版新提示词。</p>' +
    '<div class="card">' +
    '<label class="field"><span>你最想解决的问题（一两句话）</span>' +
    '<textarea id="imp-problem" style="min-height:70px">' + esc(prefill) + "</textarea></label>" +
    '<div class="btn-row"><button class="btn primary big" onclick="doImprove(' + runId + ')">生成改进版（约 1 次调用）</button></div>' +
    "</div>";
}
window.doImprove = async function (runId) {
  var problem = document.getElementById("imp-problem").value.trim();
  if (!problem) { toast("先写一两句你最想解决的问题。"); return; }
  $app.innerHTML = '<div class="card"><p>正在生成改进版…（约十几秒）</p></div>';
  try {
    var r = await api("/api/runs/" + runId + "/improve", { body: { problem_summary: problem } });
    window._cand = r;
    renderCandidate(runId);
  } catch (e) {
    toast(e.message);
    renderImprove(runId);
  }
};

/* ----- 确认新版本 ----- */

async function renderCandidate(runId) {
  var d = await api("/api/runs/" + runId);
  var run = d.run;
  var v = await api("/api/prompts/" + run.prompt_id + "/versions/" + run.candidate_version_id);
  var note = v.version.note || (window._cand && window._cand.change_note) || "";
  $app.innerHTML = "<h1>新版本出来了，先过目</h1>" +
    '<p class="sub">下面是 AI 改出的新提示词。<b>你可以直接改它</b>，改完再比。蓝框里是它说改了什么，不一定是全部改动，以你看到的为准。</p>' +
    (note ? '<div class="prompt-box" style="margin-bottom:12px">' + esc(note) + "</div>" : "") +
    '<div class="card">' +
    '<label class="field"><span>新提示词（可编辑）</span>' +
    '<textarea id="cand-content" style="min-height:220px">' + esc(v.version.content) + "</textarea></label>" +
    '<div class="btn-row"><button class="btn primary big" onclick="confirmCandidate(' + runId + ')">就用这版，开始比一比</button>' +
    '<a class="btn" href="#/prompt/' + run.prompt_id + '">先不要了</a>' +
    '<span class="muted small">接下来会给同样几个例子生成新版的输出，然后左右打乱让你盲评。</span></div>' +
    "</div>";
}
window.confirmCandidate = async function (runId) {
  var content = document.getElementById("cand-content").value.trim();
  if (!content) { toast("新提示词内容不能为空。"); return; }
  try {
    await api("/api/runs/" + runId + "/candidate", { body: { content: content } });
    await api("/api/runs/" + runId + "/step/candidate", { body: {} });
    viewRun(runId);
  } catch (e) { toast(e.message); }
};

/* ----- 盲评比较 ----- */

async function renderCompare(runId) {
  var d = await api("/api/runs/" + runId + "/pairs");
  if (!d.pairs.length) {
    $app.innerHTML = '<div class="banner warn">这轮没有可比较的成品（生成失败太多）。<a href="#/prompt/' +
      (await api("/api/runs/" + runId)).run.prompt_id + '">回去看看</a></div>';
    return;
  }
  var firstUndone = 0;
  for (var i = 0; i < d.pairs.length; i++) {
    if (!d.pairs[i].judged) { firstUndone = i; break; }
  }
  window._cmp = { pairs: d.pairs, idx: firstUndone, runId: runId };
  drawPair();
}
function drawPair() {
  var c = window._cmp;
  if (c.idx >= c.pairs.length) {
    var runId = c.runId;
    $app.innerHTML = '<div class="card"><p>全部比完了，正在算结论…</p></div>';
    setTimeout(async function () { viewRun(runId); }, 600);
    return;
  }
  var p = c.pairs[c.idx];
  var html = "<h1>比一比：第 " + (c.idx + 1) + " / " + c.pairs.length + " 份</h1>" +
    '<p class="sub">左右两份输出，一份来自原版、一份来自新版，顺序已打乱。分别判断能不能用，再说哪份好。两边都评完才揭晓。</p>' +
    '<div class="card"><details open><summary class="field-label">这轮的输入（点开/收起）</summary>' +
    '<div class="output-box" style="max-height:110px">' + esc(p.input) + "</div>" +
    (p.reference ? '<div class="field-label mt">参考材料：</div><div class="output-box" style="max-height:110px">' + esc(p.reference) + "</div>" : "") +
    "</details></div>" +
    '<div class="compare-grid">' + paneHtml("甲", "a", p.a) + paneHtml("乙", "b", p.b) + "</div>" +
    '<div class="card mt"><div class="field-label">整体哪份更好？</div><div class="opt-row" data-role="pref">';
  for (var j = 0; j < PREF.length; j++) {
    html += '<button class="opt" onclick="pickOpt(this)" data-v="' + PREF[j][0] + '">' + PREF[j][1] + "</button>";
  }
  html += "</div><div class=\"btn-row\"><button class=\"btn primary big\" onclick=\"submitPair()\">提交这份（两边都选完才能提交）</button></div></div>";
  $app.innerHTML = html;
}
function paneHtml(title, side, content) {
  var html = '<div class="pane"><div class="pane-title">' + title + " 的输出</div>" +
    '<div class="output-box">' + esc(content) + "</div>" +
    '<div class="field-label mt">' + title + " 能直接用吗？</div>" +
    '<div class="opt-row" data-role="rating" data-side="' + side + '">';
  for (var j = 0; j < USABLE.length; j++) {
    html += '<button class="opt" onclick="pickOpt(this)" data-v="' + USABLE[j][0] + '">' + USABLE[j][1] + "</button>";
  }
  html += "</div></div>";
  return html;
}
window.submitPair = async function () {
  var c = window._cmp;
  var p = c.pairs[c.idx];
  var ra = document.querySelector('.opt-row[data-side="a"] .opt.sel');
  var rb = document.querySelector('.opt-row[data-side="b"] .opt.sel');
  var pref = document.querySelector('.opt-row[data-role="pref"] .opt.sel');
  if (!ra || !rb || !pref) { toast("两边「能不能用」和「哪份更好」都要选（可以选「说不上来/说不清」）。"); return; }
  var r = await api("/api/pairs/" + p.id, { body: {
    rating_a: ra.getAttribute("data-v"), rating_b: rb.getAttribute("data-v"),
    preference: pref.getAttribute("data-v") } });
  var revealHtml = "";
  if (r.revealed !== null && r.revealed !== undefined) {
    revealHtml = '<div class="reveal">揭晓：甲是' + (r.revealed ? "新版" : "原版") +
      "，乙是" + (r.revealed ? "原版" : "新版") + "。</div>";
  }
  var card = document.querySelector(".card.mt") || document.querySelector(".card");
  card.insertAdjacentHTML("beforeend", revealHtml +
    '<div class="btn-row"><button class="btn primary big" onclick="nextPair()">下一份</button></div>');
  c.idx = c.idx + 1;
};
window.nextPair = function () { drawPair(); };

/* ----- 结论页 ----- */

function usdLabel(v) { return USABLE.filter(function (u) { return u[0] === v; }).map(function (u) { return u[1]; })[0] || v; }

async function renderVerdict(runId) {
  var d = await api("/api/runs/" + runId);
  var run = d.run;
  var v = (await api("/api/runs/" + runId + "/verdict")).verdict;
  if (!v) { toast("结论还没算好，刷新一下。"); return; }
  var c = v.counts;
  var html = "<h1>" + (run.kind === "validate" ? "考试结果" : "这轮探索的结果") + "</h1>";
  html += '<div class="card verdict"><div class="headline">' + esc(v.headline) + "</div>" +
    '<div class="lines">' + v.detail_lines.map(function (l) { return "<div>" + esc(l) + "</div>"; }).join("") + "</div>";
  if (v.sample_warning) html += '<div class="warn-line">' + esc(v.sample_warning) + "</div>";
  html += '<div class="tail">' + esc(v.tail) + "</div></div>";

  // 逐份证据：弄坏/修好
  var pairsData = (await api("/api/runs/" + runId + "/pairs")).pairs;
  function caseList(ids, title, color) {
    if (!ids || !ids.length) return "";
    var inner = "";
    for (var i = 0; i < pairsData.length; i++) {
      var p = pairsData[i];
      if (ids.indexOf(p.case_id) === -1) continue;
      inner += '<details class="mt"><summary>第 ' + (i + 1) + " 份（输入预览：" +
        esc(p.input.slice(0, 24)) + "…）</summary>" +
        '<div class="output-box mt" style="max-height:90px">' + esc(p.input) + "</div>" +
        '<div class="compare-grid mt">' +
        '<div class="pane"><div class="pane-title">甲（' + (p.a_is_candidate ? "新版" : "原版") + "：" +
        usdLabel(p.rating_a) + "）</div><div class=\"output-box\">" + esc(p.a) + "</div></div>" +
        '<div class="pane"><div class="pane-title">乙（' + (p.a_is_candidate ? "原版" : "新版") + "：" +
        usdLabel(p.rating_b) + "）</div><div class=\"output-box\">" + esc(p.b) + "</div></div>" +
        "</div></details>";
    }
    return '<h2 style="color:' + color + '">' + title + "（" + ids.length + " 份）</h2>" + inner;
  }
  html += caseList(v.regressed_case_ids, "新版弄坏的（原来是能直接用的）", "var(--bad)");
  html += caseList(v.improved_case_ids, "新版修好的", "var(--ok)");

  if (run.kind === "explore") {
    html += '<div class="card"><div class="btn-row">' +
      '<button class="btn primary big" onclick="saveTrial(' + runId + ')">存为试用版（未考试）</button>' +
      '<a class="btn" href="#/exams">了解「考个试」</a>' +
      '<a class="btn" href="#/prompt/' + run.prompt_id + '">回提示词页</a></div>' +
      '<div class="hint mt">试用版可以在「用一用」里日常试，页面会一直标着「未考试」；攒够 15—20 份新例子，再来考个试，通过才能转正。</div></div>';
  } else {
    html += '<div class="card"><div class="btn-row">';
    if (v.can_adopt) {
      html += '<button class="btn primary big" onclick="adopt(' + runId + "," + run.prompt_id + "," + run.candidate_version_id + ",false)\">转正为新版（正式版）</button>";
    } else {
      html += '<button class="btn" onclick="adoptForce(' + runId + "," + run.prompt_id + "," + run.candidate_version_id + ')">仍要转正（不推荐）</button>';
    }
    html += '<a class="btn" href="#/prompt/' + run.prompt_id + '">保留原版，回到提示词页</a></div>' +
      '<div class="hint mt">转正只是把「正式版」指针指到这一版，原版仍保留在版本历史里，随时可回滚。</div></div>';
  }
  $app.innerHTML = html;
}
window.saveTrial = async function (runId) {
  try {
    var r = await api("/api/runs/" + runId + "/save_trial", { body: {} });
    toast(r.message);
    var d = await api("/api/runs/" + runId);
    location.hash = "#/prompt/" + d.run.prompt_id;
  } catch (e) { toast(e.message); }
};
window.adopt = async function (runId, promptId, versionId, force) {
  try {
    var r = await api("/api/prompts/" + promptId + "/adopt", {
      body: { version_id: versionId, run_id: runId, force: force } });
    if (r.need_force) {
      if (window.confirm(r.message)) {
        return adopt(runId, promptId, versionId, true);
      }
      return;
    }
    toast(r.message);
    location.hash = "#/prompt/" + promptId;
  } catch (e) { toast(e.message); }
};
window.adoptForce = function (runId, promptId, versionId) {
  if (window.confirm("这一版没有通过考试。没考过试就转正，等于没验证就上岗。\n确定要这样吗？（随时可回滚）")) {
    adopt(runId, promptId, versionId, true);
  }
};

/* ---------- 提示词详情 ---------- */

async function viewPrompt(pid) {
  var d = await api("/api/prompts/" + pid);
  var p = d.prompt;
  var vById = {};
  for (var i = 0; i < d.versions.length; i++) vById[d.versions[i].id] = d.versions[i];
  var tags = "";
  if (p.official_version_id && vById[p.official_version_id]) {
    tags += '<span class="badge official">正式 v' + vById[p.official_version_id].version_no + "</span>";
  }
  if (p.trial_version_id && vById[p.trial_version_id]) {
    tags += '<span class="badge trial">试用 v' + vById[p.trial_version_id].version_no + "（未考试）</span>";
  }
  var html = "<h1>" + esc(p.name) + tags + "</h1>";

  // 优化记录
  html += "<h2>优化记录</h2>";
  if (!d.runs.length) {
    html += '<div class="card muted">还没有运行过。下面可以开始新一轮。</div>';
  } else {
    for (var r = 0; r < d.runs.length; r++) {
      var run = d.runs[r];
      var verdictNote = "";
      if (run.verdict) {
        try { verdictNote = JSON.parse(run.verdict).headline; } catch (e) { /* 忽略 */ }
      }
      html += '<div class="list-item"><div class="title">' +
        (run.kind === "validate" ? "考试" : "探索") +
        ' <span class="badge gray">' + (RUN_STATE_LABEL[run.state] || run.state) + "</span></div>" +
        '<div class="meta">' + esc(run.created_at) + (verdictNote ? " · " + esc(verdictNote) : "") + "</div>" +
        '<div class="btn-row"><a class="btn" href="#/run/' + run.id + '">打开</a></div></div>';
    }
  }

  // 再优化一轮
  html += "<h2>再优化一轮（探索）</h2>" +
    '<div class="card"><div class="field-label">以哪个版本为基准？</div><select id="opt-base">';
  var defaultBase = p.official_version_id || (d.versions.length ? d.versions[d.versions.length - 1].id : "");
  for (var vi = d.versions.length - 1; vi >= 0; vi--) {
    var vv = d.versions[vi];
    var originTxt = ORIGIN_LABEL[vv.origin] || vv.origin;
    html += '<option value="' + vv.id + '"' + (vv.id === defaultBase ? " selected" : "") + ">v" +
      vv.version_no + " " + esc(vv.label || "") + "（" + esc(originTxt) + "）</option>";
  }
  html += "</select><div class=\"field-label mt\">选 2—8 个例子（默认勾了前几个）</div>";
  for (var ci = 0; ci < d.cases.length; ci++) {
    var cc = d.cases[ci];
    html += '<label class="small" style="display:block;margin:4px 0"><input type="checkbox" class="opt-case" value="' + cc.id + '"' +
      (ci < 3 ? " checked" : "") + "> 例子：" + esc(cc.input_text.slice(0, 40)) +
      (cc.input_text.length > 40 ? "…" : "") + "（" + (cc.source === "usage" ? "日常用过的" : "贴入的") + "）</label>";
  }
  if (!d.cases.length) html += '<div class="muted small">还没有例子，先在下面「攒的例子」里贴几个。</div>';
  html += '<div class="btn-row"><button class="btn primary" onclick="startExplore(' + pid + ')">开始这一轮</button></div></div>';

  // 攒的例子
  html += "<h2>攒的例子（" + d.cases.length + " 条）</h2>" +
    '<div class="card"><label class="field"><span>快速补充：把新例子贴进来，<b>每条例子之间空一行</b></span>' +
    '<textarea id="quick-cases" placeholder="例子一\n\n例子二"></textarea></label>' +
    '<div class="btn-row"><button class="btn" onclick="quickAddCases(' + pid + ')">存入例子池</button>' +
    '<span class="muted small">在「用一用」里用过的输入，点了反馈后也会自动存进来。</span></div>';
  if (d.cases.length) {
    html += '<details class="raw mt"><summary>查看已攒的全部例子</summary>';
    for (var ci2 = 0; ci2 < d.cases.length; ci2++) {
      var c2 = d.cases[ci2];
      html += '<div class="list-item"><div class="meta"> #' + c2.id + " · " +
        (c2.source === "usage" ? "日常用过的" : "贴入的") + " · " + esc(c2.created_at) + "</div>" +
        '<div class="output-box" style="max-height:70px">' + esc(c2.input_text) + "</div></div>";
    }
    html += "</details>";
  }
  html += "</div>";

  // 版本历史
  html += "<h2>版本历史</h2><div class=\"card\"><table class=\"plain\"><tr><th>版本</th><th>标记</th><th>来源</th><th>时间</th><th>操作</th></tr>";
  for (var vi2 = 0; vi2 < d.versions.length; vi2++) {
    var v = d.versions[vi2];
    var ops = '<a href="javascript:void(0)" onclick="viewVersion(' + v.id + "," + pid + ')">查看</a> ' +
      '<a href="javascript:void(0)" onclick="makeOfficial(' + pid + "," + v.id + ',this)">设为正式</a>';
    if (p.official_version_id && v.id !== p.official_version_id) {
      ops += ' <a href="javascript:void(0)" onclick="makeOfficial(' + pid + "," + v.id + ',this)">回滚到此版</a>';
    }
    html += "<tr><td>v" + v.version_no + "</td><td>" + (v.label ? esc(v.label) : "—") + "</td><td>" +
      (ORIGIN_LABEL[v.origin] || esc(v.origin)) + "</td><td class=\"small muted\">" + esc(v.created_at) +
      "</td><td>" + ops + "</td></tr>";
  }
  html += "</table><div class=\"hint mt\">「正式版」是日常在用的版本；「试用版」还没考过试。改动不覆盖历史，回滚也不删任何东西。</div></div>";
  $app.innerHTML = html;
}
window.viewVersion = async function (vid, pid) {
  var v = (await api("/api/prompts/" + pid + "/versions/" + vid)).version;
  var html = "<h1>v" + v.version_no + " " + esc(v.label || "") + "</h1>" +
    '<p class="sub">' + (v.note ? esc(v.note) + " · " : "") + esc(v.created_at) + ' · <a href="#/prompt/' + pid + '">返回</a></p>' +
    '<div class="card"><div class="prompt-box">' + esc(v.content) + "</div>" +
    '<div class="btn-row"><button class="btn" onclick="copyText(window._v)">复制全文</button></div></div>';
  window._v = v.content;
  $app.innerHTML = html;
};
window.makeOfficial = async function (pid, vid, el) {
  if (!window.confirm("把这一版设为正式版？\n如果它没考过试，等于没验证就上岗（随时可回滚）。")) return;
  try {
    var r = await api("/api/prompts/" + pid + "/adopt", { body: { version_id: vid, force: true } });
    toast(r.message);
    viewPrompt(pid);
  } catch (e) { toast(e.message); }
};
window.startExplore = async function (pid) {
  try {
    var base = document.getElementById("opt-base").value;
    var boxes = document.querySelectorAll(".opt-case:checked");
    var ids = [];
    for (var i = 0; i < boxes.length; i++) ids.push(parseInt(boxes[i].value, 10));
    if (ids.length < 2 || ids.length > 8) { toast("选 2—8 个例子。"); return; }
    var r = await api("/api/runs", { body: { prompt_id: pid, kind: "explore",
      base_version_id: parseInt(base, 10), case_ids: ids } });
    location.hash = "#/run/" + r.run_id;
  } catch (e) { toast(e.message); }
};
window.quickAddCases = async function (pid) {
  var raw = document.getElementById("quick-cases").value.trim();
  if (!raw) { toast("先贴例子。每条例子之间空一行。"); return; }
  var parts = raw.split(/\n\s*\n/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
  try {
    var r = await api("/api/prompts/" + pid + "/cases", { body: { cases: parts.map(function (s) { return { input: s }; }) } });
    toast(r.message);
    viewPrompt(pid);
  } catch (e) { toast(e.message); }
};

/* ---------- 用一用 ---------- */

async function viewUse() {
  var d = await api("/api/use/options");
  if (!d.options.length) {
    $app.innerHTML = "<h1>用一用</h1><div class=\"empty\"><div class=\"big\">还没有提示词</div>" +
      '先去 <a href="#/new">新建优化</a> 建一条。</div>';
    return;
  }
  var html = "<h1>用一用</h1>" +
    '<p class="sub">贴上今天的真实输入，用正在使用的版本出结果。用完点一下反馈，输入会自动存进例子池。</p>' +
    '<div class="card"><label class="field"><span>用哪条提示词？</span><select id="use-prompt">';
  for (var i = 0; i < d.options.length; i++) {
    var o = d.options[i];
    html += '<option value="' + o.id + '">' + esc(o.name) + "（当前用：" + esc(o.version_label) + "）</option>";
  }
  html += "</select></label>" +
    '<label class="field"><span>今天的输入（原样贴进来）</span>' +
    '<textarea id="use-input" style="min-height:130px"></textarea></label>' +
    '<div class="btn-row"><button class="btn primary big" onclick="doUse()">出结果（1 次调用）</button>' +
    '<span class="muted small">想固定用「正式版」或「试用版」，到对应提示词页可改。</span></div>' +
    '<div id="use-result"></div></div>';
  $app.innerHTML = html;
}
window.doUse = async function () {
  var pid = parseInt(document.getElementById("use-prompt").value, 10);
  var text = document.getElementById("use-input").value.trim();
  if (!text) { toast("先把输入贴进来。"); return; }
  var box = document.getElementById("use-result");
  box.innerHTML = "<p class=\"muted\">正在生成…</p>";
  try {
    var r = await api("/api/use", { body: { prompt_id: pid, input: text } });
    window._use = r;
    box.innerHTML = '<div class="field-label mt">结果（' + esc(r.version_label) + " · " + esc(r.cost_note) + "）：</div>" +
      '<div class="output-box">' + esc(r.output) + "</div>" +
      '<div class="btn-row"><button class="btn" onclick="copyText(window._use.output)">复制结果</button></div>' +
      '<div class="field-label mt">这份结果你怎么用的？</div><div class="opt-row" data-role="fb">' +
      '<button class="opt" onclick="pickOpt(this)" data-v="as_is">直接用了</button>' +
      '<button class="opt" onclick="pickOpt(this)" data-v="edited">改了改</button>' +
      '<button class="opt" onclick="pickOpt(this)" data-v="bad">没用上</button></div>' +
      '<div id="fb-extra"></div>' +
      '<div class="btn-row"><button class="btn primary" onclick="submitFeedback()">提交反馈</button>' +
      '<label class="small"><input type="checkbox" id="fb-pool" checked> 同时把这条输入存进例子池</label></div>';
  } catch (e) {
    box.innerHTML = '<div class="banner warn">' + esc(e.message) + "</div>";
  }
};
window.submitFeedback = async function () {
  var sel = document.querySelector('.opt-row[data-role="fb"] .opt.sel');
  if (!sel) { toast("先点一个反馈（直接用了/改了改/没用上）。"); return; }
  var v = sel.getAttribute("data-v");
  var extra = document.getElementById("fb-extra");
  var edited = "";
  if (v === "edited") {
    var ta = extra.querySelector("textarea");
    if (!ta) {
      extra.innerHTML = '<label class="field mt"><span class="muted small">把你改后的文本贴进来（下次对比就有据可查）</span>' +
        '<textarea id="fb-edited"></textarea></label>';
      toast("贴一下你改后的文本，再点提交。");
      return;
    }
    edited = ta.value.trim();
    if (!edited) { toast("改后文本还空着。"); return; }
  }
  try {
    var r = await api("/api/use/" + window._use.use_id + "/feedback", { body: {
      result: v, edited_text: edited || undefined,
      add_to_pool: document.getElementById("fb-pool").checked } });
    toast(r.message);
    document.getElementById("use-input").value = "";
    document.getElementById("use-result").innerHTML = "";
  } catch (e) { toast(e.message); }
};

/* ---------- 考个试 ---------- */

async function viewExams() {
  var d = await api("/api/exams");
  var html = "<h1>考个试</h1>" +
    '<p class="sub">试用版要「转正」，先考试：用<b>没考过的新例子</b>，把正式版（或原版）和试用版放一起盲评。建议 20 份以上，至少 15 份；少于 15 份只能当参考。</p>';
  if (!d.exams.length) {
    html += '<div class="empty"><div class="big">还没有待考试的试用版</div>' +
      '先去 <a href="#/new">新建优化</a> 做一轮探索，把改好的提示词存为试用版。</div>';
    $app.innerHTML = html;
    return;
  }
  for (var i = 0; i < d.exams.length; i++) {
    var e = d.exams[i];
    var lastNote = "";
    if (e.last_verdict) lastNote = " · 上次结论：" + e.last_verdict.headline;
    html += '<div class="list-item"><div class="title">' + esc(e.name) +
      '<span class="badge trial">试用 v' + e.trial_v + "（未考试）</span>" +
      (e.official_v != null ? '<span class="badge official">正式 v' + e.official_v + "</span>" : "") +
      "</div>" +
      '<div class="meta">没考过的新例子：' + e.fresh_cases + " 份 · 考过 " + e.validated + " 次" + esc(lastNote) + "</div>" +
      '<label class="field mt"><span class="small muted">例子不够？快速补充（每条例子之间空一行，考完的例子不能重复用）</span>' +
      '<textarea id="exam-cases-' + e.prompt_id + '" style="min-height:70px"></textarea></label>' +
      '<div class="btn-row"><button class="btn" onclick="quickAddCases(' + e.prompt_id + ')">存入例子池</button>' +
      '<button class="btn primary" onclick="startExam(' + e.prompt_id + "," + e.trial_version_id + "," + (e.official_version_id || "null") + "," + e.fresh_cases + ')">开始考试</button>' +
      (e.last_run_id ? '<a class="btn" href="#/run/' + e.last_run_id + '">看上次结果</a>' : "") +
      "</div></div>";
  }
  $app.innerHTML = html;
}
window.startExam = async function (pid, trialVid, officialVid, freshCount) {
  try {
    if (freshCount < 15) {
      if (!window.confirm("现在没考过的新例子只有 " + freshCount + " 份（建议 20 份以上，至少 15 份）。\n份太少，考完了也只能说「还看不出来」。仍要现在考吗？")) return;
    }
    var detail = await api("/api/prompts/" + pid);
    var ids = [];
    var trialIdNum = trialVid;
    // 基准：正式版；没有就用最新的非试用版本
    var base = officialVid;
    if (!base) {
      for (var i = detail.versions.length - 1; i >= 0; i--) {
        if (detail.versions[i].id !== trialIdNum) { base = detail.versions[i].id; break; }
      }
    }
    // 找没考过的新例子
    var validateIds = {};
    for (var r = 0; r < detail.runs.length; r++) {
      if (detail.runs[r].kind === "validate") validateIds[detail.runs[r].id] = true;
    }
    for (var c = 0; c < detail.cases.length; c++) {
      var cs = detail.cases[c];
      var used = [];
      try { used = JSON.parse(cs.used_in || "[]"); } catch (e2) { /* 忽略 */ }
      var usedInValidate = false;
      for (var u = 0; u < used.length; u++) { if (validateIds[used[u]]) { usedInValidate = true; break; } }
      if (!usedInValidate) ids.push(cs.id);
    }
    if (ids.length < 5) { toast("没考过的新例子不足 5 份，先补充例子再考。"); return; }
    var resp = await api("/api/runs", { body: { prompt_id: pid, kind: "validate",
      base_version_id: base, candidate_version_id: trialIdNum, case_ids: ids } });
    location.hash = "#/run/" + resp.run_id;
  } catch (e) { toast(e.message); }
};

/* ---------- 花费与记录 ---------- */

async function viewLedger() {
  var d = await api("/api/ledger");
  var t = d.total;
  var html = "<h1>花费与记录</h1>" +
    '<div class="card"><div class="kv">' +
    '<div class="k">累计成功调用</div><div>' + t.requests + " 次</div>" +
    '<div class="k">累计消耗</div><div>输入 ' + t.tokens_in + " + 输出 " + t.tokens_out + " tokens</div>" +
    '<div class="k">估算总花费</div><div>' + (t.cost_est != null ? fmtCost(t.cost_est) : "未填单价，只记次数和 tokens") + "</div>" +
    '<div class="k">本月调用</div><div>' + d.month.requests + " 次" +
    (d.month.cost_est != null ? " · " + fmtCost(d.month.cost_est) : "") + "</div></div>" +
    '<div class="hint mt">每次调模型都会记在这里，包括优化、考试、日常使用和测试连接。设置好单价就有金额估算。</div></div>';
  html += '<div class="card"><table class="plain"><tr><th>时间</th><th>用途</th><th>输入</th><th>输出</th><th>估算</th><th>状态</th></tr>';
  if (!d.entries.length) {
    html += '<tr><td colspan="6" class="muted">还没有调用记录。</td></tr>';
  }
  for (var i = 0; i < d.entries.length; i++) {
    var e = d.entries[i];
    html += "<tr><td class=\"small muted\">" + esc(e.ts) + "</td><td>" + esc(e.purpose) + "</td><td>" +
      e.prompt_tokens + "</td><td>" + e.completion_tokens + "</td><td>" +
      (e.est_cost != null ? fmtCost(e.est_cost) : "—") + "</td><td>" +
      (e.status === "ok" ? "成功" : '<span style="color:var(--bad)">失败</span>') + "</td></tr>";
  }
  html += "</table></div>";
  $app.innerHTML = html;
}

/* ---------- 设置 ---------- */

async function viewSettings() {
  var d = await api("/api/settings");
  var s = d.settings;
  var moneyMode = s.cap_type === "money";
  $app.innerHTML = "<h1>设置</h1>" +
    '<p class="sub">只需要配一次。所有信息都保存在你自己的电脑上（data/lab.db），不会发到别处。</p>' +
    '<div class="card"><h2 style="margin-top:0">AI 接口</h2>' +
    '<label class="field"><span>接口地址</span>' +
    '<input type="text" id="st-base" value="' + esc(s.api_base) + '" placeholder="例如：https://api.openai.com/v1（一般以 /v1 结尾）"></label>' +
    '<label class="field"><span>API 密钥 ' + (s.api_key ? '<span class="badge official">已保存</span>' : "") + "</span>" +
    '<input type="password" id="st-key" placeholder="' + (s.api_key ? "已保存。留空表示不修改；换密钥才需要重填。" : "粘贴你的密钥") + '"></label>' +
    '<label class="field"><span>模型名</span>' +
    '<input type="text" id="st-model" value="' + esc(s.model) + '" placeholder="例如：gpt-4o-mini 或其他你开通的模型名"></label>' +
    '<div class="btn-row"><button class="btn" onclick="testConn()">测试连接</button>' +
    '<span id="test-result" class="small"></span></div></div>' +
    '<div class="card"><h2 style="margin-top:0">花费上限（每轮优化）</h2>' +
    '<div class="field-label">到上限自动停，已完成的部分都保留。</div>' +
    '<div class="opt-row" style="margin-bottom:10px">' +
    '<button class="opt' + (moneyMode ? "" : " sel") + '" onclick="setCapType(this,\'requests\')">按次数（推荐）</button>' +
    '<button class="opt' + (moneyMode ? " sel" : "") + '" onclick="setCapType(this,\'money\')">按金额（需先填单价）</button></div>' +
    '<div id="cap-requests" style="' + (moneyMode ? "display:none" : "") + '">' +
    '<label class="field"><span>每轮优化最多调用几次？</span>' +
    '<input type="number" id="st-cap-r" value="' + esc(s.cap_value) + '" min="3" max="500">' +
    '<div class="hint">默认 40 次。探索一轮 2—8 个例子通常用 3—9 次，足够了。</div></label></div>' +
    '<div id="cap-money" style="' + (moneyMode ? "" : "display:none") + '">' +
    '<label class="field"><span>每轮优化最多花多少钱（元）？</span>' +
    '<input type="number" id="st-cap-m" value="' + esc(s.cap_value) + '" min="0.5" step="0.5"></label></div>' +
    '<div class="kv" style="margin-top:8px"><div class="k">输入单价</div><div>' +
    '<input type="number" id="st-pi" value="' + esc(s.price_in) + '" step="0.1" placeholder="元 / 百万输入tokens，不知道就留空">' +
    "</div><div class=\"k\">输出单价</div><div>" +
    '<input type="number" id="st-po" value="' + esc(s.price_out) + '" step="0.1" placeholder="元 / 百万输出tokens，不知道就留空">' +
    "</div></div></div>" +
    '<div class="btn-row"><button class="btn primary big" onclick="saveSettings()">保存设置</button></div>';
  window._capType = s.cap_type;
}
window.setCapType = function (btn, t) {
  window._capType = t;
  var row = btn.closest(".opt-row");
  var btns = row.querySelectorAll(".opt");
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove("sel");
  btn.classList.add("sel");
  document.getElementById("cap-requests").style.display = t === "requests" ? "" : "none";
  document.getElementById("cap-money").style.display = t === "money" ? "" : "none";
};
window.testConn = async function () {
  var el = document.getElementById("test-result");
  el.textContent = "测试中…";
  try {
    var r = await api("/api/settings/test", { body: {
      api_base: document.getElementById("st-base").value.trim(),
      api_key: document.getElementById("st-key").value.trim() || true,
      model: document.getElementById("st-model").value.trim() } });
    el.innerHTML = r.ok ? '<span style="color:var(--ok)">' + esc(r.message) + "</span>"
      : '<span style="color:var(--bad)">' + esc(r.message) + "</span>";
  } catch (e) {
    el.innerHTML = '<span style="color:var(--bad)">' + esc(e.message) + "</span>";
  }
};
window.saveSettings = async function () {
  var keyVal = document.getElementById("st-key").value.trim();
  var body = {
    api_base: document.getElementById("st-base").value.trim(),
    api_key: keyVal || true,
    model: document.getElementById("st-model").value.trim(),
    cap_type: window._capType || "requests",
    cap_value: window._capType === "money"
      ? (document.getElementById("st-cap-m").value || "5")
      : (document.getElementById("st-cap-r").value || "40"),
    price_in: document.getElementById("st-pi").value,
    price_out: document.getElementById("st-po").value
  };
  try {
    await api("/api/settings", { body: body });
    toast("已保存。");
    route();
  } catch (e) { toast(e.message); }
};

/* ---------- 启动 ---------- */
route();

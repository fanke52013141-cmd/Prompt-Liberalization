/* 提示词优化实验室 本地版界面（优化1.0）。
   主线为五步流程：准备材料 → 确认怎么评 → 原始测评 → 自动优化 → 验证与使用；
   原内部模块（标注/评价器/实验配置等）归入"高级功能"。
   所有动态内容经 esc() 转义后渲染（TC051：恶意HTML不执行）。 */
"use strict";

const API = "/workflow-api/v1";
const state = { projects: [], pid: null, templates: {} };
const FLOW_STEPS = [
  { key: "prepare", name: "准备材料", page: "materials" },
  { key: "confirm_eval", name: "确认怎么评", page: "evaluate" },
  { key: "baseline", name: "原始测评", page: "baseline" },
  { key: "optimize", name: "自动优化", page: "optimize" },
  { key: "verify", name: "验证与使用", page: "verify" },
];
const FLOW_PAGE = Object.fromEntries(FLOW_STEPS.map(s => [s.key, s.page]));
const PAGES = {};

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function api(method, path, body, headers) {
  const opt = { method, headers: Object.assign({"Content-Type": "application/json"}, headers || {}) };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(API + path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) {
    const msg = data && data.message ? `${data.code}：${data.message}` : `HTTP ${res.status}`;
    const err = new Error(msg); err.body = data; throw err;
  }
  return data;
}
function toast(msg, isErr) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = isErr ? "err" : "";
  t.classList.remove("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 5200);
}
function pill(text, cls) { return `<span class="pill ${cls || ""}">${esc(text)}</span>`; }
function errText(e) {
  const fe = e.body && e.body.field_errors
    ? "\n" + Object.entries(e.body.field_errors).map(([k, v]) => `${k}: ${v}`).join("\n") : "";
  return e.message + fe;
}
/* 统一显示：本地时间 / 中文来源 / 中文任务名（避免内部标识直接暴露给用户） */
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function originLabel(o) {
  return { manual: "人工", optimizer: "优化候选", trial: "试用" }[o] || (o === "人工" ? o : "人工");
}
const TASK_LABELS = {
  student_feedback: "学员作答点评", question_explain: "题目解析",
  article_title: "公众号标题", article_framework: "文章框架",
};
function taskLabel(p) {
  return (p.contract && p.contract.label) || TASK_LABELS[p.task_type] || p.task_type;
}
function cleanNote(t) {
  // 后端 note 里的内部规范编号（§9.2 / TC043 等）对用户没有意义，统一隐藏
  return String(t == null ? "" : t)
    .replace(/（?\s*§[^）]*）?/g, "")
    .replace(/（?\s*TC\d+[^）]*）?/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/* ---------------- 统一模态框：替代原生 confirm（全站一致的确认体验） ---------------- */
function uiConfirm(opt) {
  return new Promise(resolve => {
    const tone = opt.tone || "ask";
    const ico = { ask: "?", warn: "!", danger: "!" }[tone];
    const wrap = document.createElement("div");
    wrap.className = "modal-overlay";
    wrap.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="m-title"><span class="m-ico ${tone}">${ico}</span>${esc(opt.title || "请确认")}</div>
        <div class="m-body">${esc(opt.message || "")}</div>
        <div class="m-foot">
          <button class="grey" data-act="cancel">${esc(opt.cancelText || "取消")}</button>
          <button class="${tone === "danger" ? "danger" : ""}" data-act="ok">${esc(opt.okText || "确定")}</button>
        </div>
      </div>`;
    const done = v => { wrap.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = e => { if (e.key === "Escape") done(false); };
    wrap.addEventListener("click", e => {
      if (e.target === wrap) done(false);
      const act = e.target.getAttribute && e.target.getAttribute("data-act");
      if (act === "ok") done(true);
      if (act === "cancel") done(false);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    wrap.querySelector('[data-act="ok"]').focus();
  });
}

/* ---------------- 术语：页面内虚线词悬停即解释 + 名词解释页 ---------------- */
const GLOSSARY = [
  ["项目", "针对一个具体业务场景（如“学员作答点评”）建立的长期优化工作区。", "反复优化时，材料、标准和版本都能在同一个项目里积累与复用。"],
  ["输入字段", "每次调用执行 AI 时要提供给它的信息（题目、学员答案等）。", "提示词里用 {{字段名}} 引用它们，系统按案例逐条替换成真实内容。"],
  ["评价专用字段", "只给评价用的资料（参考答案、专家批注等）。", "系统保证它们永远不会发给执行 AI，防止它照着参考答案作答。"],
  ["优化目标", "你现在最不满意的地方、希望改善的方向，自由描述。", "没有明确目标，优化就只能盲目提分，无法针对真正的问题。"],
  ["基线", "你当前正在使用的提示词版本。", "所有“变好了吗”的比较都以它为起点，改坏了能随时退回。"],
  ["候选", "系统在优化中改写出的新版本提示词。", "只有通过底线检查（原会的不退步、严重错误不增加）才有资格被保留。"],
  ["保留 / 淘汰", "每轮改写后系统对候选的取舍决定。", "全部写出具体依据（提升多少、有无回退），可追溯、可复核。"],
  ["开发集", "系统反复用来测试和改写的案例，相当于“练习题”。", "让系统从真实案例中学习问题模式，而不是凭空想象。"],
  ["封存测试集", "优化期间系统接触不到的案例，相当于“考题”。", "防止只是把练习题背会了；最后用它检验是否真的变好。"],
  ["冻结切分", "把案例分组锁定：同一来源不会既当练习题又当考题。", "保证独立检验的公正性；锁定后本批案例不可换组。"],
  ["评价标准", "“怎样算好”的具体规定：检查维度与 0-3 分锚点。", "由你确认后发布，系统优化期间不会私自更改成功标准。"],
  ["评价器", "按评价标准给输出打分的模型或流程。", "标准或模型变化后会标记过期，需重新校准才能继续批量使用。"],
  ["专家意见", "人工指出的具体问题（保留原话与期望表现）。", "这是优化最直接的依据，比笼统的分数更能指明改法。"],
  ["ABCD 评级", "单个案例的改善分级：A 全解决 / B 部分解决 / C 未解决 / D 出现新问题。", "统一沟通口径，同时展示原问题改善与新增问题两个维度。"],
  ["原始测评", "优化前把基线在开发集上完整测一遍。", "先摸清现状、拿到带证据的问题清单，优化才有明确靶子。"],
  ["独立验证", "用封存考题对候选与基线做最终对照测试。", "通过才有“验证有效”；没通过或证据不足都会如实显示。"],
  ["账本与预算", "每次模型调用的用量与费用记录。", "花费透明可控：预算耗尽自动暂停，验证预留单独保护。"],
  ["演示模式", "内置离线模拟供应商（无需 API Key）。", "不花一分钱就能跑通全部流程、看懂每一步在做什么。"],
];
function term(k) {
  const d = GLOSSARY.find(g => g[0] === k);
  return `<span class="term" title="${esc(d ? d[1] : k)}">${esc(k)}</span>`;
}
function pageGlossary() {
  setMain(`<h1>名词解释</h1>
  <p class="sub">每个词两句话：它是什么，为什么需要。页面里的<span class="term" title="像这样：鼠标放上来就能看到解释。">虚线词</span>悬停即可看解释。</p>
  <div class="gloss-grid">`
    + GLOSSARY.map(([k, what, why], idx) => `
    <div class="card gloss-card">
      <div class="gloss-no">${String(idx + 1).padStart(2, "0")}</div>
      <b>${esc(k)}</b>
      <div class="gloss-block what"><span>【是什么】</span>${esc(what)}</div>
      <div class="gloss-block why"><span>【为什么需要】</span>${esc(why)}</div>
    </div>`).join("") + `</div>`);
}

/* ---------------- 导航 ---------------- */
const ADV_PAGES = [
  ["data", "案例与数据"], ["rubric", "评价标准"], ["annotation", "人工标注"],
  ["judge", "评价器校准"], ["prompts", "提示词库"], ["playground", "试运行"],
  ["experiment", "实验配置"], ["runs", "运行记录"], ["acceptance", "验收报告"],
  ["usage", "使用与反馈"],
];
function renderNav() {
  const h = location.hash || "#/projects";
  let html = `<a href="#/projects" class="${h === "#/projects" || h.startsWith("#/proj/") ? "active" : ""}">项目列表</a>`;
  if (state.pid) {
    html += `<div class="group">优化流程</div>`;
    for (const s of FLOW_STEPS) {
      const active = h === `#/proj/${state.pid}/${s.page}` ? "active" : "";
      html += `<a href="#/proj/${state.pid}/${s.page}" class="${active}">${esc(s.name)}</a>`;
    }
    html += `<a href="#/proj/${state.pid}/home" class="${h.endsWith("/home") ? "active" : ""}">项目主页</a>`;
    html += `<div class="group">高级功能</div>`;
    for (const [key, label] of ADV_PAGES) {
      const active = h === `#/proj/${state.pid}/${key}` ? "active" : "";
      html += `<a href="#/proj/${state.pid}/${key}" class="${active}">${esc(label)}</a>`;
    }
  }
  html += `<div class="group">系统</div><a href="#/settings" class="${h === "#/settings" ? "active" : ""}">模型设置</a>`
    + `<a href="#/glossary" class="${h === "#/glossary" ? "active" : ""}">名词解释</a>`;
  document.getElementById("nav").innerHTML = html;
}

/* ---------------- 路由（§12：运行/版本/报告均可恢复直达） ---------------- */
async function route() {
  const h = location.hash || "#/projects";
  let m = h.match(/^#\/proj\/([^/]+)\/run\/([^/]+)$/);
  if (m) { state.pid = m[1]; await pageRunDetail(m[2]); renderNav(); return; }
  m = h.match(/^#\/proj\/([^/]+)\/([^/]+)$/);
  if (m) { state.pid = m[1]; await pageProject(m[2]); }
  else if (h === "#/settings") { state.pid = null; await pageSettingsGlobal(); }
  else if (h === "#/glossary") { state.pid = null; pageGlossary(); }
  else { state.pid = null; await pageProjects(); }
  renderNav();
  window.scrollTo(0, 0);
}
function setMain(html) { document.getElementById("main").innerHTML = html; }

/* ---------------- 项目列表（显示真实状态与下一步 §4.1） ---------------- */
async function pageProjects() {
  const [pr] = await Promise.all([api("GET", "/projects"), api("GET", "/templates")]);
  state.projects = pr.projects;
  const hasDemo = pr.projects.some(p => p.name.startsWith("示例："));
  const cards = [];
  for (const p of pr.projects) {
    let step = "";
    try {
      const prog = await api("GET", `/projects/${p.id}/progress`);
      const cur = prog.steps.find(s => s.key === prog.current) || prog.steps[0];
      step = `<div class="muted small">下一步：${esc(cur.action)}（${esc(cur.explain)}）</div>`;
    } catch (e) { /* 显示基础信息 */ }
    cards.push(`
    <div class="card">
      <div class="flex">
        <div style="flex:2">
          <b>${esc(p.name)}</b> ${pill(esc(taskLabel(p)), "brand")}
          ${p.name.startsWith("示例：") ? pill("演示", "warn") : ""}
          ${p.status === "archived" ? pill("已归档", "warn") : pill("使用中", "ok")}
          <div class="muted small">${esc([p.description, "创建于 " + fmtTime(p.created_at)].filter(Boolean).join(" · "))}</div>
          ${step}
        </div>
        <div style="flex:0">
          <button onclick="openProject('${p.id}')">进入</button>
          ${p.status === "archived"
            ? `<button class="grey" onclick="unarchive('${p.id}')">取消归档</button>`
            : `<button class="grey" onclick="archive('${p.id}')">归档</button>`}
          <button class="danger-text" title="彻底删除该项目及其全部数据，不可恢复"
            onclick="deleteProject('${p.id}')">删除</button>
        </div>
      </div>
    </div>`);
  }
  const tplOpts = [["student_feedback", "学员作答点评"], ["question_explain", "题目解析"],
    ["article_title", "公众号标题"], ["article_framework", "文章框架"]]
    .map(([k, label]) => `<option value="${k}">${esc(label)}（可选示例）</option>`).join("");
  setMain(`
    <h1>提示词优化实验室</h1>
    <div class="card valueprop">
      <b>这个工具帮你做什么</b>
      <p style="margin:6px 0">你有一段正在用的提示词（比如让 AI 点评学员作业），但输出总在某些地方不满意。
      把它交给本系统，再给几个真实案例，系统会：
      <span class="flow-chips"><span class="flow-chip">先测现状</span><span class="arrow">→</span><span class="flow-chip">按你确认的标准自动改写</span><span class="arrow">→</span><span class="flow-chip">用没参与修改的新案例独立验证</span></span>
      最后你拿走一段改进后的提示词，和一份说明“改了什么、效果如何、证据是什么”的报告。</p>
      <div class="grid3">
        <div class="vp-col green"><div class="vp-head"><span>它会</span><span class="vp-badge">自动化支持</span></div>
          <ul class="vp-list">
          <li>先测出提示词现在的水平</li>
          <li>按<b>你确认的</b>标准自动改写、测试、取舍</li>
          <li>守住底线：原来会的不能变差</li>
          <li>用“考题”独立检验，如实报告结论</li></ul></div>
        <div class="vp-col orange"><div class="vp-head"><span>它不会</span><span class="vp-badge">安全与权责</span></div>
          <ul class="vp-list">
          <li>不保证一定变好——没提升会如实说“未见提升，保留原版”</li>
          <li>不偷改你的成功标准</li>
          <li>不把参考答案泄露给执行 AI</li>
          <li>不用“背会练习题”冒充真的有效</li></ul></div>
        <div class="vp-col blue"><div class="vp-head"><span>你要准备</span><span class="vp-badge">课前准备项</span></div>
          <ul class="vp-list">
          <li>正在用的提示词原文</li>
          <li>几条真实案例（有专家指过问题的最好）</li>
          <li>一句话说明最不满意什么</li></ul></div>
      </div>
      <div style="margin-top:10px">
        <button id="demo-btn" onclick="openDemo()">${hasDemo ? "打开示例项目" : "看一遍示例（约1分钟，用演示数据，不花钱）"}</button>
        <button class="ghost" onclick="focusCreate()">直接用我的真实材料开始 ↓</button>
        <span class="muted small">不确定怎么用？先看示例：里面材料、专家意见、优化过程、验证报告全是现成的。</span>
      </div>
    </div>
    <h2 style="font-size:15px">我的项目</h2>
    ${cards.join("") || `<div class="card empty">还没有项目。先看一遍示例，或在下面用真实材料创建第一个。</div>`}
    <div class="card" id="create-card">
      <b>新建项目</b>
      <p class="muted small">创建后进入第一步“准备材料”；任务模板只是可选示例，推荐自定义任务——只填几个输入框，结构由系统自动拼装。</p>
      <div class="flex">
        <div><label>项目名称</label><input id="np-name" placeholder="例如：初中学员点评优化"></div>
        <div><label>任务来源</label><select id="np-source" onchange="npSourceChanged()">
          <option value="custom">自定义任务（推荐）</option>
          <option value="template">从示例模板开始</option></select></div>
      </div>
      <div id="np-custom">
        <div class="flex">
          <div><label>任务名称（如：学员作答点评）</label><input id="np-label" placeholder="你想优化的一件事"></div>
          <div><label>评价单位</label><input id="np-unit" placeholder="如：一份完整点评"></div>
        </div>
        <div class="muted small" style="margin-top:10px"><b>① 输入字段</b>——每次调用 AI 时要提供给它的信息。
          提示词里用 <code>{{字段名}}</code> 引用，例如：请点评 {{题目}} 与 {{学员答案}}。</div>
        <div id="np-runtime-rows"></div>
        <button class="grey" onclick="npAddField('runtime')">＋ 添加输入字段</button>
        <div class="muted small" style="margin-top:12px"><b>② 评价专用字段（选填）</b>——只给“评价/检查”用的资料，
          例如参考答案、专家批注。系统保证它们<b>永远不会发给执行模型</b>，避免 AI 抄参考答案。</div>
        <div id="np-eval-rows"></div>
        <button class="grey" onclick="npAddField('eval')">＋ 添加评价专用字段</button>
        <details style="margin-top:8px"><summary>预览：系统会自动把上面的输入框拼装成以下任务结构（无需手写）</summary>
          <pre id="np-preview"></pre></details>
      </div>
      <div id="np-template" class="hidden">
        <div class="flex"><div><label>示例模板</label><select id="np-task">${tplOpts}</select></div></div>
        <p class="muted small">模板自带输入契约与评价标准草案（演示数据标识 is_demo），创建后可调整。</p>
      </div>
      <label>优化目标（当前主要问题、希望改善的地方，可随时修改）</label>
      <textarea id="np-goal" style="min-height:60px" placeholder="例如：列式正确但计算错误时，点评经常错误归因为概念不清，希望准确区分。"></textarea>
      <div style="margin-top:10px"><button onclick="createProject()">创建项目</button></div>
    </div>`);
  npSeedRows();
  npUpdatePreview();
}
function npSourceChanged() {
  const v = document.getElementById("np-source").value;
  document.getElementById("np-custom").classList.toggle("hidden", v !== "custom");
  document.getElementById("np-template").classList.toggle("hidden", v === "custom");
  if (v === "custom") { npSeedRows(); npUpdatePreview(); }
}
async function openDemo() {
  const btn = document.getElementById("demo-btn");
  if (btn) { btn.disabled = true; btn.textContent = "正在生成示例（几秒钟）…"; }
  try {
    const r = await api("POST", "/demo/seed");
    toast(r.created ? "示例项目已生成：材料、专家意见、优化与报告都是现成的" : "示例项目已存在，直接打开");
    location.hash = `#/proj/${r.project_id}/home`;
  } catch (e) {
    toast(errText(e), true);
    if (btn) { btn.disabled = false; btn.textContent = "看一遍示例（约1分钟，用演示数据，不花钱）"; }
  }
}
function focusCreate() {
  const el = document.getElementById("np-name");
  if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.focus(); }
}
/* 字段行编辑器：用户只填输入框，系统自动拼装成任务结构（§5.2 易用性） */
function npFieldRow(kind, label, name) {
  return `<div class="flex np-row" data-kind="${kind}" style="margin-top:6px">
    <div><label>显示名</label><input class="np-label" value="${esc(label || "")}" placeholder="如：学员答案"
      oninput="npUpdatePreview()"></div>
    <div><label>字段名（提示词中写 {{字段名}}；留空则按显示名自动生成）</label>
      <input class="np-name" value="${esc(name || "")}" placeholder="如：student_answer"
      oninput="npUpdatePreview()"></div>
    <div style="flex:0 0 110px"><label>类型</label><select class="np-type">
      <option value="text">文本</option><option value="enum">枚举</option>
      <option value="integer">整数</option><option value="number">数字</option></select></div>
    <div style="flex:0 0 64px"><label> </label>
      <button class="grey" onclick="this.closest('.np-row').remove();npUpdatePreview()">删除</button></div>
  </div>`;
}
function npAddField(kind, label, name) {
  const box = document.getElementById(kind === "runtime" ? "np-runtime-rows" : "np-eval-rows");
  box.insertAdjacentHTML("beforeend", npFieldRow(kind, label, name));
  npUpdatePreview();
}
function npSeedRows() {
  const rt = document.getElementById("np-runtime-rows");
  const ev = document.getElementById("np-eval-rows");
  if (!rt.children.length) {
    rt.innerHTML = npFieldRow("runtime", "题目", "question") + npFieldRow("runtime", "学员答案", "student_answer");
  }
  if (!ev.children.length) {
    ev.innerHTML = npFieldRow("eval", "参考答案", "expert_answer");
  }
}
function npCollectFields(kind) {
  const rows = [...document.querySelectorAll(`.np-row[data-kind="${kind}"]`)];
  const out = [];
  for (const row of rows) {
    const label = row.querySelector(".np-label").value.trim();
    let name = row.querySelector(".np-name").value.trim();
    if (!label && !name) continue;  // 空行忽略
    if (!name) name = label.replace(/[\s{}"'，,：:]/g, "") || `字段${out.length + 1}`;
    const bad = /[\s{}"'，,：:]/.test(name);
    if (bad) throw new Error(`字段名「${name}」不能包含空格、花括号、逗号或冒号；请修改后重试`);
    out.push({ name, label: label || name, type: row.querySelector(".np-type").value });
  }
  return out;
}
function npUpdatePreview() {
  const el = document.getElementById("np-preview");
  if (!el) return;
  try {
    const structure = {
      任务: document.getElementById("np-label").value || "（任务名称）",
      输入字段_交给执行模型: npCollectFields("runtime").map(f => `${f.name}(${f.label})`),
      评价专用字段_绝不发给执行模型: npCollectFields("eval").map(f => `${f.name}(${f.label})`),
    };
    el.textContent = JSON.stringify(structure, null, 2);
  } catch (e) { el.textContent = e.message; }
}
async function createProject() {
  const name = document.getElementById("np-name").value.trim();
  if (!name) { toast("项目名称不能为空", true); return; }
  const source = document.getElementById("np-source").value;
  const goal = document.getElementById("np-goal").value;
  const body = { name, description: "", goal };
  if (source === "custom") {
    let runtime, evaluation;
    try {
      runtime = npCollectFields("runtime");
      evaluation = npCollectFields("eval");
    } catch (e) { toast(e.message, true); return; }
    if (!runtime.length) { toast("至少添加一个输入字段（AI 每次需要看到的信息）", true); return; }
    const dup = runtime.find(f => evaluation.some(x => x.name === f.name));
    if (dup) { toast(`字段「${dup.name}」不能同时作为输入字段和评价专用字段`, true); return; }
    body.task_type = "custom";
    body.contract = {
      label: document.getElementById("np-label").value.trim() || name,
      runtime_fields: runtime,
      evaluation_fields: evaluation,
      evaluation_unit: document.getElementById("np-unit").value.trim() || "一份完整输出",
    };
  } else {
    body.task_type = document.getElementById("np-task").value;
  }
  try {
    const p = await api("POST", "/projects", body);
    toast("项目已创建");
    location.hash = `#/proj/${p.id}/home`;
  } catch (e) { toast(errText(e), true); }
}
function openProject(id) { location.hash = `#/proj/${id}/home`; }
async function deleteProject(id) {
  const p = (state.projects || []).find(x => x.id === id);
  const okDel = await uiConfirm({ title: "删除项目", tone: "danger", okText: "彻底删除",
    message: `确定删除项目「${p ? p.name : id}」？\n\n项目下的案例、运行、报告将一并删除，不可恢复。` });
  if (!okDel) return;
  try {
    await api("DELETE", `/projects/${id}`);
    toast("项目已删除");
    route();
  } catch (e) { toast(errText(e), true); }
}
async function archive(id) { await api("POST", `/projects/${id}/archive`); toast("已归档：历史可读，新收费运行将被拒绝"); route(); }
async function unarchive(id) { await api("POST", `/projects/${id}/unarchive`); toast("已恢复"); route(); }

/* ---------------- 项目页头与五步流程条（§4.2） ---------------- */
async function pageProject(page) {
  const p = await api("GET", `/projects/${state.pid}`);
  const head = `<div class="flex" style="align-items:baseline">
      <div style="flex:1"><h1>${esc(p.name)}</h1>
      <p class="sub">${pill(esc(p.contract.label || p.task_type), "brand")}
      ${p.status === "archived" ? pill("已归档", "warn") : ""}
      ${esc(p.contract.evaluation_unit || "")}</p></div>
      <div style="flex:0"><a class="small" href="#/projects">← 返回项目列表</a></div></div>`
    + (p.name.startsWith("示例：")
      ? `<div class="card demo-banner"><b>这是一个演示项目</b>：<span class="small">所有数据由内置模拟供应商生成，均为演示性质。
        材料是现成的、优化已跑完、报告已生成——随便点开每一步看懂流程，然后用「新建项目」换成你的真实材料。</span></div>`
      : "");
  const fn = PAGES[page];
  if (!fn) { setMain(head + `<div class="card empty">页面不存在：<b>${esc(page)}</b>。请从左侧菜单选择。</div>`); return; }
  const body = await fn(p);
  setMain(head + body);
}
async function getProgress(pid) { return api("GET", `/projects/${pid}/progress`); }
function stepBar(progress, pid) {
  return `<div class="steps">` + FLOW_STEPS.map(s => {
    const st = progress.steps.find(x => x.key === s.key) || {};
    const mark = st.done ? "✓" : (progress.current === s.key ? "●" : "○");
    const cls = st.done ? "done" : (progress.current === s.key ? "cur" : "");
    return `<a class="step ${cls}" href="#/proj/${pid}/${s.page}" title="${esc(st.why || "")}"><span class="mark">${mark}</span>${esc(s.name)}</a>`;
  }).join(`<span class="arrow">→</span>`) + `</div>`;
}
/* 三段式引导：这一步解决什么 / 你要提供什么 / 做完之后（§1.2 每步说明目的与结果） */
function advIntro(text) {
  return `<div class="card intro"><div class="intro-row"><span class="intro-k">这一页做什么</span><span>${text}</span></div></div>`;
}
function flowIntro(what, provide, then) {
  return `<div class="card intro">
    <div class="intro-row"><span class="intro-k">这一步解决什么</span><span>${what}</span></div>
    <div class="intro-row"><span class="intro-k">你现在要做的</span><span>${provide}</span></div>
    <div class="intro-row"><span class="intro-k">做完之后</span><span>${then}</span></div>
  </div>`;
}

/* ---------------- 项目主页（§4.2） ---------------- */
PAGES.home = async (p) => {
  const prog = await getProgress(p.id);
  const cur = prog.steps.find(s => s.key === prog.current);
  const page = FLOW_PAGE[cur.key];
  const materials = [p.contract.runtime_fields.length ? "输入字段 " + p.contract.runtime_fields.map(f => f.label).join("、") : "",
    p.contract.evaluation_fields.length ? "评价字段 " + p.contract.evaluation_fields.map(f => f.label).join("、") : "",
    p.contract.goal ? "优化目标已填写" : ""].filter(Boolean).join("；") || "暂无材料";
  return `
  ${stepBar(prog, p.id)}
  <div class="card focus">
    <div class="muted small">现在需要你做的事</div>
    <h2 style="margin:4px 0">${esc(cur.action)}</h2>
    <div class="muted small">为什么要做</div>
    <div>${esc(cur.why)}</div>
    <div class="muted small" style="margin-top:8px">做完之后</div>
    <div class="small">${esc(cur.then || "")}</div>
    <div class="muted small" style="margin-top:8px">已有材料</div>
    <div class="small">${esc(materials)}</div>
    <div style="margin-top:12px"><a href="#/proj/${p.id}/${page}"><button>${esc(cur.action)} →</button></a>
    ${cur.key === "optimize" ? `<a href="#/proj/${p.id}/runs"><button class="grey">查看优化进度</button></a>` : ""}
    ${cur.key === "verify" && prog.steps[4].done && cur.action !== "查看结果报告" ? `<a href="#/proj/${p.id}/verify"><button class="grey">查看结果报告</button></a>` : ""}</div>
  </div>
  <div class="card">
    <b>辅助入口</b>
    <div class="flex">
      <a href="#/proj/${p.id}/materials" class="small">本次材料</a>
      <a href="#/proj/${p.id}/evaluate" class="small">评价规则与专家意见</a>
      <a href="#/proj/${p.id}/runs" class="small">历次优化</a>
      <a href="#/proj/${p.id}/prompts" class="small">提示词版本</a>
      <a href="#/glossary" class="small">名词解释</a>
    </div>
    <p class="muted small">优化目标：${esc(p.contract.goal || "（尚未填写，可在“准备材料”中补充）")}</p>
  </div>`;
};

/* ---------------- 第一步：准备材料（§5） ---------------- */
PAGES.materials = async (p) => {
  const prog = await getProgress(p.id);
  const [items, ps] = await Promise.all([
    api("GET", `/projects/${p.id}/items?size=100`),
    api("GET", `/projects/${p.id}/prompts`)]);
  const rt = p.contract.runtime_fields.map(f => esc(f.label)).join("、");
  const ev = p.contract.evaluation_fields.map(f => esc(f.label)).join("、") || "（无）";
  return `
  ${stepBar(prog, p.id)}
  ${flowIntro("告诉系统“优化什么”：你现在的提示词，以及真实使用时会遇到的案例。",
    "① 在下方粘贴提示词原文；② 粘贴或导入几条真实案例（有专家指过问题的最好）；③ 用一句话写下你最不满意的地方。",
    "系统会检查材料、标出每个字段的用途；案例就绪后进入第二步“确认怎么评”。")}
  <div class="card">
    <p class="muted">提供你正在使用的提示词，以及一些实际使用案例。已有 AI 输出或专家意见，也可以一起导入。</p>
    <label>优化目标——一句话说明现在最不满意什么（自由描述，随时可改）</label>
    <textarea id="mat-goal" style="min-height:60px" placeholder="例：学员列式正确但计算出错时，点评经常说成“概念不清”；希望先区分列式思路与计算，再给结论。">${esc(p.contract.goal || "")}</textarea>
    <div style="margin-top:6px"><button class="grey" onclick="saveGoal()">保存优化目标</button>
    <span id="goal-status" class="muted small"></span></div>
  </div>
  <div class="card">
    <b>当前提示词</b>
    ${ps.prompts.length ? pill(`已有 ${ps.prompts.length} 个版本`, "ok") : pill("尚无提示词", "warn")}
    ${ps.prompts.length ? `
      <table><tr><th>名称</th><th>版本</th><th>来源</th><th>长度</th><th></th></tr>
      ${ps.prompts.slice(0, 5).map(v => `<tr><td>${esc(v.name)}</td><td>v${v.version_no}</td>
        <td>${pill(v.origin === "optimizer" ? "优化候选" : "人工", v.origin === "optimizer" ? "brand" : "")}</td>
        <td class="small">${v.length || esc(String(v.body.length))} 字</td>
        <td><a class="small" href="#/proj/${p.id}/prompts">管理</a></td></tr>`).join("")}</table>`
      : `<label>粘贴当前提示词正文（提示词里用 {{字段名}} 引用你定义的输入字段，系统会自动替换成每个案例的真实内容）</label>
         <textarea id="mat-prompt" placeholder="例：&#10;你是一名教研老师。请根据题目与学员答案，给出一份作业点评：先判断对错并说明依据，再指出具体错在哪一步，最后给出可执行的修改建议。"></textarea>
         <label>提示词名称</label><input id="mat-prompt-name" value="基线提示词">
         <label>使用的变量（逗号分隔，必须属于输入字段：${esc(p.contract.runtime_fields.map(f => f.name).join("、"))}）</label>
         <input id="mat-prompt-vars" value="${esc(p.contract.runtime_fields.map(f => f.name).join(","))}">
         <div class="muted small">这就是${term("基线")}——一切比较的起点；没有现成提示词时可先建初稿，系统会明确标记“无可比较的原始版本”。</div>
         <div style="margin-top:8px"><button onclick="createBaselinePrompt()">创建初稿版本</button></div>`}
  </div>
  <div class="card">
    <b>实际案例与已有结果</b>
    <p class="muted small">把真实案例交给系统（相当于${term("开发集")}练习题）。每行一条 JSON：
    case_id 是案例编号；runtime_input 里放${term("输入字段")}的内容；evaluation_only 里放${term("评价专用字段")}（如参考答案）。
    不确定格式就照着下面的占位示例抄，或用一条真实案例试跑“预览校验”——系统会逐行告诉你哪里要改。</p>
    <div class="flex">
      <div style="flex:0 0 140px"><label>格式</label>
        <select id="imp-fmt"><option value="jsonl">JSONL</option><option value="csv">CSV（runtime:前缀）</option></select></div>
      <div style="flex:3"><label>内容（每行一条 JSON，或 CSV 文本）</label>
        <textarea id="imp-content" placeholder='{"case_id":"c001","runtime_input":{"question":"解方程 2x+3=11","student_answer":"x=4","grade_level":"初中"},"evaluation_only":{"expert_answer":"x=4，正确"}}
{"case_id":"c002", … 第二条案例 }'></textarea></div>
    </div>
    <div style="margin-top:8px">
      <button onclick="importPreview()">预览校验</button>
      <button class="ghost" onclick="importCommit()">提交导入</button>
      <span id="imp-result"></span>
    </div>
    <pre id="imp-preview" class="hidden"></pre>
  </div>
  <div class="card">
    <b>字段用途确认（系统建议，可修改）</b>
    <div class="check"><span class="dot" style="color:var(--ok)">✓</span>
      <span style="flex:1">交给执行模型：<b>${rt || "（未定义）"}</b></span></div>
    <div class="check"><span class="dot" style="color:var(--warn)">✓</span>
      <span style="flex:1">只用于评价（绝不发给执行模型）：<b>${ev}</b></span></div>
    <div class="check"><span class="dot" style="color:var(--brand)">✓</span>
      <span style="flex:1">已有案例：<b>${items.total}</b> 条
      <a class="small" href="#/proj/${p.id}/data">查看与分配集合</a></span></div>
    <p class="muted small">字段名不能独自决定用途：参考答案等资料能否提供给执行模型，由实际业务确认。</p>
  </div>
  <div class="card">
    <b>锁定案例分组（用于最后的独立检验）</b>
    <p class="muted small">系统会把案例分成“练习题”（${term("开发集")}）和“考题”（${term("封存测试集")}）并${term("冻结切分")}：
    考题在优化期间系统碰不到，最后用来检验改好的提示词是不是真有效。锁定后本批案例不能换组。</p>
    <button class="grey" onclick="freezeManifest()">锁定案例分组（冻结切分）</button>
    <span class="muted small">没有手动分配过也不用担心：点锁定时系统会给出“练习 / 考题”的自动划分建议，确认后生效。
    想自己控制划分，可到「高级功能 → 案例与数据」逐条指定。</span>
  </div>
  ${(() => {
    const ready = [!!p.contract.goal, ps.prompts.length > 0, items.total > 0,
      !!prog.steps.find(x => x.key === "prepare").done];
    const n = ready.filter(Boolean).length;
    return `<div class="action-bar">
      <a href="#/projects"><button class="grey">← 保存并返回</button></a>
      <span class="ready"><span class="ready-bar"><i style="width:${Math.round(n / 4 * 100)}%"></i></span>
        材料就绪度 ${n}/4（目标${ready[0] ? "✓" : "○"} 提示词${ready[1] ? "✓" : "○"} 案例${ready[2] ? "✓" : "○"} 分组锁定${ready[3] ? "✓" : "○"}）</span>
      <span class="grow"></span>
      <a href="#/proj/${p.id}/evaluate"><button>下一步：确认怎么评 →</button></a>
    </div>`;
  })()}`;
};
async function saveGoal() {
  await api("PUT", `/projects/${state.pid}/goal`, { goal: document.getElementById("mat-goal").value });
  document.getElementById("goal-status").textContent = "已保存 ✓";
  toast("优化目标已保存");
}
async function createBaselinePrompt() {
  try {
    await api("POST", `/projects/${state.pid}/prompts`, {
      name: document.getElementById("mat-prompt-name").value.trim() || "基线提示词",
      body: document.getElementById("mat-prompt").value,
      variables: document.getElementById("mat-prompt-vars").value.split(",").map(s => s.trim()).filter(Boolean),
      frozen_segments: [], params: {} });
    toast("初稿版本已创建（不改变当前使用指针）");
    route();
  } catch (e) { toast(errText(e), true); }
}
async function importPreview() {
  const fmt = document.getElementById("imp-fmt").value;
  const content = document.getElementById("imp-content").value;
  const b = await api("POST", `/projects/${state.pid}/imports/preview`, { fmt, content });
  window._lastBatch = b;
  document.getElementById("imp-preview").classList.remove("hidden");
  document.getElementById("imp-preview").textContent =
    `批次 ${b.id}\n总行数 ${b.total}，有效 ${b.valid}，错误 ${b.errors.length}\n` +
    (b.errors.map(e => `第${e.line}行 ${e.case_id || ""}：${(e.reasons || [e.reason]).join("；")}`).join("\n") || "无错误行");
  document.getElementById("imp-result").innerHTML =
    b.valid > 0 ? pill(`预览成功：有效 ${b.valid} 条，可提交`, "ok") : pill("无有效行", "bad");
}
async function importCommit() {
  const b = window._lastBatch;
  if (!b) { toast("请先点击“预览校验”，确认无误后再提交", true); return; }
  if (b.valid === 0) {
    toast("没有可导入的有效行：请看下方预览里逐行标出的问题（常见原因：字段名与项目定义的输入字段不一致），修正后重新预览", true);
    return;
  }
  if (b.errors.length) {
    const okPart = await uiConfirm({ title: "部分行存在问题", tone: "warn",
      okText: `只导入 ${b.valid} 条有效行`,
      message: `本批有 ${b.errors.length} 行存在问题，将被跳过（不会静默丢弃，明细见预览）。\n确定只导入 ${b.valid} 条有效行？` });
    if (!okPart) return;
  }
  const r = await api("POST",
    `/projects/${state.pid}/imports/${b.id}/commit`, { exclude_case_ids: [] });
  toast(`导入完成：本批有效 ${r.valid} 条已进入案例库`);
  route();
}
async function freezeManifest() {
  try {
    const pid = state.pid;
    const listing = await api("GET", `/projects/${pid}/items?size=200`);
    const items = listing.items || [];
    if (!items.length) { toast("请先导入至少 2 条案例，再锁定分组（每条案例默认各自成组）", true); return; }
    // 按来源分组；未分配的组给出“练习/考题”自动划分建议，经确认后写入
    const groups = {};
    for (const it of items) (groups[it.source_group_id] = groups[it.source_group_id] || []).push(it);
    const gids = Object.keys(groups).sort();
    const unassigned = gids.filter(g => groups[g].some(i => i.split === "unassigned"));
    if (gids.length < 2) {
      toast(`目前只有 ${gids.length} 个来源分组。考题（封存测试）至少需要 1 个与练习案例不同的来源，` +
        "请再导入至少 1 条案例后再锁定。", true);
      return;
    }
    let plan = null;
    if (unassigned.length) {
      const nSeal = Math.max(1, Math.floor(unassigned.length / 3));
      plan = { dev: unassigned.slice(0, unassigned.length - nSeal), sealed: unassigned.slice(unassigned.length - nSeal) };
      const msg = "锁定前需要把案例分成两组角色：\n\n" +
        "· 练习案例（开发集）：优化过程中反复使用；\n" +
        "· 考题（封存测试集）：优化期间系统接触不到，最后用来独立验证。\n\n" +
        `检测到 ${unassigned.length} 个来源分组未分配。建议自动划分：\n` +
        `· 练习 ${plan.dev.length} 组（${plan.dev.join("、")}）\n` +
        `· 考题 ${plan.sealed.length} 组（${plan.sealed.join("、")}）\n\n` +
        "采用建议并锁定？（也可先到「高级功能 → 案例与数据」手动调整）";
      const okGo = await uiConfirm({ title: "锁定案例分组", tone: "ask",
        okText: "采用建议并锁定", message: msg });
      if (!okGo) return;
      for (const g of plan.dev) {
        await api("POST", `/projects/${pid}/split`,
          { case_ids: groups[g].map(i => i.id), split: "dev" });
      }
      for (const g of plan.sealed) {
        await api("POST", `/projects/${pid}/split`,
          { case_ids: groups[g].map(i => i.id), split: "sealed_test" });
      }
    }
    const r = await api("POST", `/projects/${pid}/manifests/freeze`, { seed: 20260927 });
    if (!r.sealed_test_items) {
      toast("已锁定，但没有案例被划为考题——最后的独立验证将无法进行。" +
        "建议在「案例与数据」把部分组改为封存测试后重新确认。", true);
    } else {
      toast(`已锁定：练习案例按分组冻结，考题 ${r.sealed_test_items} 条已封存（优化期间系统接触不到）`);
    }
    route();
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 第二步：确认怎么评（§6） ---------------- */
function fbStatusLabel(s) {
  return { pending: "待确认", confirmed_error: "已确认错误", preference: "偏好建议",
    unverified: "待核实", resolved: "已解决", retired: "已停用" }[s] || s;
}
PAGES.evaluate = async (p) => {
  const prog = await getProgress(p.id);
  const [rs, fbs, tags, rules, items, sugg] = await Promise.all([
    api("GET", `/projects/${p.id}/rubrics`),
    api("GET", `/projects/${p.id}/expert_feedback`),
    api("GET", `/projects/${p.id}/tags`),
    api("GET", `/projects/${p.id}/rating_rules`),
    api("GET", `/projects/${p.id}/items?size=100`),
    api("GET", `/projects/${p.id}/expert_feedback/suggestions`).catch(() => null)]);
  const pub = rs.rubrics.find(r => r.status === "published");
  const itemOpts = items.items.map(i => `<option value="${i.id}">${esc(i.case_id)}</option>`).join("");
  const fbRows = fbs.feedback.map(f => `
    <tr><td class="small">${esc(f.problem)}${f.quote ? `<div class="muted small">原话：${esc(f.quote)}</div>` : ""}</td>
    <td class="small">${esc(f.expected || "-")}</td>
    <td>${pill(f.severity === "severe" ? "严重" : f.severity === "preference" ? "偏好" : "一般",
        f.severity === "severe" ? "bad" : "")}</td>
    <td>${pill(fbStatusLabel(f.status), f.status === "confirmed_error" ? "bad" : f.status === "pending" ? "warn" : "")}</td>
    <td class="small">${esc((f.tags || []).join("、"))}</td>
    <td class="small">${esc(f.remark || "")}</td>
    <td>${f.status !== "confirmed_error" ? `<button class="grey" onclick="confirmFeedback('${f.id}')">确认为错误</button>` : ""}</td></tr>`).join("");
  const tagRows = tags.tags.map(t => `
    <tr><td>${esc(t.name)}</td><td class="small">${esc(t.definition || "-")}</td>
    <td>${t.merged_into ? pill("已合并", "warn") : t.active ? pill("启用", "ok") : pill("停用", "")}</td>
    <td>${t.active && !t.merged_into ? `<button class="grey" onclick="retireTag('${t.id}')">停用</button>` : ""}</td></tr>`).join("");
  const rule = rules.rules[rules.rules.length - 1];
  return `
  ${stepBar(prog, p.id)}
  ${flowIntro("和系统约定“怎样算改好了”。标准由你确认，系统优化期间不会私自更改。",
    "① 创建/发布评价标准（系统按你的任务生成草案，改改就能用）；② 把专家指出的问题登记成检查项（保留原话）；③ 看一眼默认的 ABCD 评级规则，可自定义。",
    "生成一份运行摘要；确认无误后，就可以开始原始测评了。")}
  <div class="card">
    <b>为什么要做这一步</b>
    <p class="muted small">在修改前约定怎样判断改善，避免系统自行改变成功标准。系统根据材料生成可编辑建议，不要求你从零搭建评价体系。</p>
    评价标准：${pub ? pill(`已发布 v${pub.version_no}`, "ok") + `<a class="small" href="#/proj/${p.id}/rubric"> 查看/编辑</a>`
      : pill("未发布", "warn") + ` <a class="small" href="#/proj/${p.id}/rubric"> 去创建并发布</a>`}
  </div>
  <div class="card">
    <b>专家意见 → 待确认检查项</b>
    <p class="muted small">保留专家原话；一条意见拆分为可检查的问题与期望。注意区分：<b>学员得分</b>是 AI 对学员作答的业务评分（待检查的输出），
    <b>点评质量评价</b>是专家对 AI 点评质量的判断（优化依据）——两者不混用。</p>
    <div class="flex">
      <div><label>案例</label><select id="fb-item">${itemOpts}</select></div>
      <div style="flex:2"><label>问题描述</label><input id="fb-problem" placeholder="如：计算错误被归因为概念不清"></div>
      <div style="flex:2"><label>专家原话/引用（可选）</label><input id="fb-quote"></div>
    </div>
    <div class="flex">
      <div style="flex:2"><label>期望表现</label><input id="fb-expected" placeholder="如：先区分列式思路与计算错误"></div>
      <div><label>影响程度</label><select id="fb-sev"><option value="severe">严重</option>
        <option value="normal" selected>一般</option><option value="preference">偏好建议</option></select></div>
      <div><label>标签（逗号分隔）</label><input id="fb-tags" placeholder="错误归因,扣分"></div>
      <div style="flex:2"><label>备注（保留原文）</label><input id="fb-remark"></div>
    </div>
    <div style="margin-top:8px"><button onclick="addFeedback()">添加专家意见</button>
    <details class="small muted" style="margin-top:6px"><summary>批量导入已有专家评价（JSONL）</summary>
      <textarea id="fb-import" placeholder='{"case_id":"c001","problem":"...","expected":"...","severity":"severe","tags":["..."],"remark":"..."}'></textarea>
      <button class="grey" onclick="importFeedback()">导入</button></details></div>
    ${fbs.feedback.length ? `<table><tr><th>问题</th><th>期望</th><th>程度</th><th>状态</th><th>标签</th><th>备注</th><th></th></tr>${fbRows}</table>`
      : `<p class="muted small">尚无专家意见。可在上方添加，或运行原始测评后由系统按评价标准诊断。</p>`}
    ${sugg && sugg.aspects && sugg.aspects.length ? `<div class="small" style="margin-top:8px"><b>系统归纳的检查方面</b>：
      ${sugg.aspects.map(a => `${esc(a.tag)}（${a.count}条${a.severe ? "，含严重" + a.severe + "条" : ""}）`).join("；")}
      <span class="muted">——需人工确认后作为检查项；标签描述现象，不自动等同于原因。</span></div>` : ""}
  </div>
  <div class="card">
    <b>标签库</b>
    <div class="flex">
      <div><label>新标签</label><input id="tag-name" placeholder="名称"></div>
      <div style="flex:2"><label>定义（描述现象，不定义原因）</label><input id="tag-def"></div>
      <div style="flex:0"><label> </label><button class="grey" onclick="addTag()">添加</button></div>
    </div>
    ${tags.tags.length ? `<table><tr><th>标签</th><th>定义</th><th>状态</th><th></th></tr>${tagRows}</table>` : ""}
  </div>
  <div class="card">
    <b>评级规则（默认 ABCD，可自定义）</b>
    ${rule ? `${pill(`v${rule.version_no}`, "brand")} ${pill(rule.status === "published" ? "已发布" : "草稿", rule.status === "published" ? "ok" : "warn")}
      <table><tr><th>等级</th><th>含义</th><th>展示依据</th></tr>
      ${rule.levels.map(l => `<tr><td><b>${esc(l.code)}</b> ${esc(l.name)}</td><td class="small">${esc(l.meaning)}</td>
        <td class="small">${esc(l.display_basis)}</td></tr>`).join("")}</table>
      <p class="muted small">${esc(rule.note || "")} 规则修改产生新版本，历史评价不被重新解释。</p>`
      : `<p class="muted small">尚未创建评级规则。ABCD 仅为首个支持方案：A=原错误全部避免、B=部分避免、C=没有避免、D=出现更多问题或明显退步；严重新增问题优先判D；始终同时展示原问题改善与新增问题两个维度。</p>
         <button onclick="createRatingRule()">创建 ABCD 评级规则草案</button>`}
  </div>
  <div class="card">
    <b>试评一个案例（检查评价方式是否合理）</b>
    <p class="muted small">展示判定和依据，允许专家确认或纠正。单个试评用于理解与调试，不等于自动评价器已经可靠。</p>
    <div class="flex">
      <div><label>案例</label><select id="rv-item">${itemOpts}</select></div>
      <div style="flex:0"><label> </label><button class="grey" onclick="suggestReview()">生成评级建议</button></div>
    </div>
    <div id="rv-suggest"></div>
  </div>
  <div class="card">
    <b>运行摘要（开始前确认）</b>
    <div class="kv">
      <div>执行/评价模型</div><div>内置离线模拟供应商（演示模式，与真实调用明显区分）；<a class="small" href="#/settings">接入真实模型</a></div>
      <div>优先目标</div><div>${esc(p.contract.goal || "（建议先在第一步填写优化目标）")}</div>
      <div>不能退步的要求</div><div>原有正确案例不得回退；严重错误不得增加（服务端强制检查）</div>
      <div>运行上限</div><div>轮数与token预算在启动时设定；预算耗尽自动暂停，验收预留单独保护</div>
      <div>需要人工介入的情况</div><div>人工参与模式下每轮等待指定评价；自动模式对关键分歧请求人工处理</div>
    </div>
    <div style="margin-top:10px">
      <a href="#/proj/${p.id}/materials"><button class="grey">返回材料</button></a>
      <a href="#/proj/${p.id}/baseline"><button>开始原始测评 →</button></a>
    </div>
  </div>`;
};
async function addFeedback() {
  const item = document.getElementById("fb-item").value;
  try {
    await api("POST", `/projects/${state.pid}/expert_feedback`, {
      item_id: item, problem: document.getElementById("fb-problem").value,
      quote: document.getElementById("fb-quote").value,
      expected: document.getElementById("fb-expected").value,
      severity: document.getElementById("fb-sev").value,
      tags: document.getElementById("fb-tags").value.split(/[，,]/).map(s => s.trim()).filter(Boolean),
      remark: document.getElementById("fb-remark").value, status: "confirmed_error" });
    toast("专家意见已登记（原话与备注保留原文）");
    route();
  } catch (e) { toast(errText(e), true); }
}
async function confirmFeedback(fid) {
  await api("PUT", `/expert_feedback/${fid}`, { status: "confirmed_error" });
  toast("已确认；该问题将进入优化目标"); route();
}
async function importFeedback() {
  try {
    const r = await api("POST", `/projects/${state.pid}/expert_feedback/import`,
      { content: document.getElementById("fb-import").value });
    toast(`导入 ${r.created} 条；${r.errors.length ? "错误 " + r.errors.length + " 条（逐行显示，不静默丢弃）" : "无错误"}`);
    route();
  } catch (e) { toast(errText(e), true); }
}
async function addTag() {
  try { await api("POST", `/projects/${state.pid}/tags`,
    { name: document.getElementById("tag-name").value, definition: document.getElementById("tag-def").value });
    route(); } catch (e) { toast(errText(e), true); }
}
async function retireTag(tid) { await api("POST", `/tags/${tid}/retire`); route(); }
async function createRatingRule() {
  const r = await api("POST", `/projects/${state.pid}/rating_rules`);
  toast("ABCD 评级规则草案已创建：" + r.id);
  route();
}
async function suggestReview() {
  const rules = await api("GET", `/projects/${state.pid}/rating_rules`);
  const rule = rules.rules[rules.rules.length - 1];
  if (!rule) { toast("请先创建评级规则", true); return; }
  const item = document.getElementById("rv-item").value;
  const s = await api("POST", `/projects/${state.pid}/case_reviews/suggest`,
    { item_id: item, rule_id: rule.id });
  const box = document.getElementById("rv-suggest");
  box.innerHTML = `
    <div class="check" style="margin-top:8px"><span class="dot">●</span>
      <span style="flex:1">建议评级 <b>${esc(s.rating)}</b> —— ${esc(s.basis)}
      <div class="muted small">${esc(s.note)}</div></span></div>
    ${s.resolutions.map(r => `<div class="small muted">问题「${esc(r.problem)}」→ ${esc(r.status)}</div>`).join("")}
    <div style="margin-top:6px"><button onclick="confirmReview('${item}','${rule.id}','${s.rating}')">确认该评级（人工确认）</button></div>`;
}
async function confirmReview(item, rule, rating) {
  try {
    await api("POST", `/projects/${state.pid}/case_reviews`,
      { item_id: item, rule_id: rule, rating, source: "human_confirmed" });
    toast("点评质量评价已记录（来源：人工确认）");
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 第三步：原始测评（§7） ---------------- */
function isBaselineRun(r) {
  const o = (r.snapshot && r.snapshot.optimization) || {};
  return !parseInt(o.max_rounds || o.max_candidates || 0);
}
PAGES.baseline = async (p) => {
  const prog = await getProgress(p.id);
  const [runs, ps, rs] = await Promise.all([
    api("GET", `/projects/${p.id}/runs`), api("GET", `/projects/${p.id}/prompts`),
    api("GET", `/projects/${p.id}/rubrics`)]);
  const baselineRuns = runs.runs.filter(r => isBaselineRun(r));
  const latest = baselineRuns.find(r => r.state === "completed");
  const pvOpts = ps.prompts.map(v =>
    `<option value="${v.id}">${esc(v.name)} v${v.version_no}（${esc(originLabel(v.origin))}）</option>`).join("") ||
    "<option value=\"\">（尚未创建提示词——请回第一步准备材料）</option>";
  const rubOpts = rs.rubrics.filter(r => r.status === "published")
    .map(r => `<option value="${r.id}">v${r.version_no}（已发布）</option>`).join("") ||
    "<option value=\"\">（尚未发布评价标准——请回第二步确认怎么评）</option>";
  let problemsHtml = "";
  if (latest) {
    const probs = [];
    for (const [iid, per] of Object.entries(latest.baseline_problems || {})) {
      for (const fbk of (per.items || [])) probs.push({ iid, ...fbk });
    }
    problemsHtml = probs.length ? `
      <table><tr><th>案例</th><th>核查的问题</th><th>自动核查</th></tr>
      ${probs.map(x => `<tr><td class="small">${esc(x.iid.slice(0, 12))}…</td>
        <td class="small">${esc(x.feedback_id)}</td><td>${pill(x.status === "resolved" ? "已解决" :
        x.status === "partial" ? "部分存在" : x.status === "unresolved" ? "仍存在" : "无法判断", "warn")}</td></tr>`).join("")}</table>
      <p class="muted small">先展示主要问题；每条问题的证据可在运行事件流与输出详情中追溯。</p>`
      : `<p class="muted small">原始测评已完成。该项目尚未登记专家问题，系统按评价标准维度诊断；可回到“确认怎么评”补充专家意见。</p>`;
  }
  return `
  ${stepBar(prog, p.id)}
  ${flowIntro("先测现状：用你确认的标准，把现在的提示词完整测一遍，看清它差在哪。",
    "选好基线提示词和评价标准（都会自动带出），点“运行原始测评”。内置演示供应商几秒出结果。",
    "得到一份问题清单和每条问题的证据；确认这些问题确实是你想解决的，再开始自动优化。")}
  <div class="card">
    <b>为什么做原始测评</b>
    <p class="muted small">建立优化起点，确认系统发现的问题确实是你要解决的问题。导入的历史输出只有来源与配置满足比较要求时才能用于正式前后对照；否则用于诊断，必要时重新生成可比输出。</p>
    <div class="flex">
      <div><label>基线提示词版本</label><select id="bl-prompt">${pvOpts}</select></div>
      <div><label>评价标准（已发布）</label><select id="bl-rubric">${rubOpts}</select></div>
      <div style="flex:0"><label> </label><button onclick="startBaselineRun()"
        title="把当前提示词在开发案例上完整测一遍">运行原始测评</button></div>
    </div>
  </div>
  <div class="card">
    <b>测评结果与问题清单</b>
    ${latest ? `<div class="kv">
      <div>原始平均分</div><div>${(latest.baseline_score || 0).toFixed(3)}</div>
      <div>运行</div><div class="small"><a href="#/proj/${p.id}/run/${latest.id}">${esc(latest.id)}</a>
      （可独立访问，刷新后仍是对应运行）</div></div>${problemsHtml}`
      : `<p class="muted small">尚未运行原始测评。点击上方“运行原始测评”：系统按已确认的评价方案测评原始提示词。</p>`}
  </div>
  <div class="card">
    <b>下一步</b>
    <p class="muted small">确认问题清单后，按这些问题开始自动优化；后续规则稳定的运行，可预先选择原始测评后自动继续。</p>
    <a href="#/proj/${p.id}/optimize"><button ${latest ? "" : "disabled"}>按这些问题开始自动优化 →</button></a>
  </div>`;
};
async function startBaselineRun() {
  try {
    await startRunCommon({ max_rounds: 0, dev_sample_size: 8, min_delta: 0.02 });
    toast("原始测评已启动（离开页面不影响运行）");
    route();
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 运行启动公共逻辑 ---------------- */
async function startRunCommon(opt) {
  const [ps, rs, mans] = await Promise.all([
    api("GET", `/projects/${state.pid}/prompts`),
    api("GET", `/projects/${state.pid}/rubrics`),
    api("GET", `/projects/${state.pid}/manifests`)]);
  const pvEl = document.getElementById("bl-prompt") || document.getElementById("ex-pv");
  const rubEl = document.getElementById("bl-rubric") || document.getElementById("ex-rubric");
  const baseline = (pvEl && pvEl.value) || (ps.prompts[ps.prompts.length - 1] || {}).id;
  const rubric = (rubEl && rubEl.value) ||
    ((rs.rubrics.filter(r => r.status === "published").slice(-1)[0]) || {}).id;
  if (!baseline || !rubric) { throw new Error("需要先准备提示词并发布评价标准（第二步）"); }
  let devIds = window._devIds;
  if (!devIds || !devIds.length) {
    devIds = (await api("GET", `/projects/${state.pid}/items?split=dev&size=100`)).items.map(i => i.id);
  }
  if (!devIds.length) { throw new Error("需要先导入案例并分配到开发集（dev）"); }
  const draft = {
    mode: "explore", prompt: { baseline_id: baseline }, rubric_id: rubric, judge_id: null,
    manifest_id: (mans.manifests[0] || {}).id || "",
    data: { dev_item_ids: devIds, select_item_ids: [] },
    models: { generation: { connection_id: "conn_mock" }, evaluation: { connection_id: "conn_mock" },
              optimizer: { connection_id: "conn_mock" } },
    optimization: opt,
    budget: { mode: "token", total_limit: 2_000_000, search_limit: 1_200_000, acceptance_limit: 600_000 },
  };
  const r = await api("POST", `/projects/${state.pid}/runs`, draft,
    { "Idempotency-Key": "ui-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8) });
  return r;
}

/* ---------------- 第四步：自动优化（§8） ---------------- */
PAGES.optimize = async (p) => {
  const prog = await getProgress(p.id);
  const [ps, rs] = await Promise.all([
    api("GET", `/projects/${p.id}/prompts`),
    api("GET", `/projects/${p.id}/rubrics`)]);
  const pubRub = rs.rubrics.filter(r => r.status === "published");
  const pvOpts = ps.prompts.map(v => `<option value="${v.id}">${esc(v.name)} v${v.version_no}（${esc(originLabel(v.origin))}）</option>`).join("");
  const rubOpts = pubRub.map(r => `<option value="${r.id}">v${r.version_no}（已发布）</option>`).join("");
  return `
  ${stepBar(prog, p.id)}
  ${flowIntro(`系统自动“改一点 → 测一遍”反复尝试，只保留真的变好的版本（${term("候选")}）。`,
    "设置轮数上限等参数——默认值就能开始；可选“人工参与”模式（每轮停下等你确认）。",
    "每一轮都有记录：为什么改、改了什么、效果如何、为什么保留或淘汰；结束后锁定一个待验证版本。")}
  <div class="card">
    <b>启动自动优化</b>
    <p class="muted small">系统按“查看失败案例 → 提出修改假设 → 修改提示词 → 重新测评 → 比较并保留版本”循环执行；
    默认在已确认的方案和上限内连续执行。停止条件：达到目标、连续无值得保留的改善、达到轮数上限、主动停止或无法继续的错误。</p>
    <div class="grid2">
      <div><label>基线提示词版本</label><select id="ex-pv">${pvOpts}</select></div>
      <div><label>评价标准（已发布）</label><select id="ex-rubric">${rubOpts}</select></div>
      <div><label>优化轮数上限</label><input id="op-rounds" type="number" value="3"></div>
      <div><label>连续无改善停止（轮）</label><input id="op-stall" type="number" value="2"></div>
      <div><label>最小改善阈值</label><input id="op-delta" type="number" step="0.01" value="0.05"></div>
      <div><label>提示词长度上限（字）</label><input id="op-len" type="number" value="4000"></div>
      <div><label>开发集抽样</label><input id="op-sample" type="number" value="8"></div>
      <div><label>人工参与模式</label><select id="op-human"><option value="">自动连续执行</option>
        <option value="1">每轮等待人工评价</option></select></div>
    </div>
    <div style="margin-top:10px"><button onclick="startOptimizeRun()">启动自动优化</button>
    <span class="muted small">内置演示供应商不花钱；预算已设上限：token 总额 200 万（搜索 120 万 + 独立验证预留 60 万，
    验证预留单独保护，不会被优化用掉）。接入真实模型或调整预算见 <a class="small" href="#/proj/${p.id}/experiment">实验配置</a>。</span></div>
  </div>
  ${await renderRunList(p)}`;
};
async function startOptimizeRun() {
  try {
    const r = await startRunCommon({
      max_rounds: +document.getElementById("op-rounds").value || 3,
      stall_rounds: +document.getElementById("op-stall").value || 2,
      min_delta: +document.getElementById("op-delta").value || 0.05,
      length_limit_chars: +document.getElementById("op-len").value || 4000,
      dev_sample_size: +document.getElementById("op-sample").value || 8,
      human_in_loop: !!document.getElementById("op-human").value,
    });
    toast("自动优化已启动：" + r.id);
    location.hash = `#/proj/${state.pid}/run/${r.id}`;
  } catch (e) { toast(errText(e), true); }
}
function runStateLabel(s) {
  return { queued: "排队", running: "运行中", waiting_human: "等待人工参与", paused_budget: "预算暂停",
    stopping: "停止中", completed: "已完成", failed: "失败", cancelled: "已取消" }[s] || s;
}
function runStopLabel(s) {
  return { candidate_found: "发现保留候选", no_improvement: "无提升（保留基线）", stalled_no_gain: "连续无改善",
    rewrite_stalled: "改写连续失败", target_reached: "达到目标", budget_exhausted: "预算耗尽",
    user_cancelled: "主动停止" }[s] || (s || "-");
}
async function renderRunList(p) {
  const runs = await api("GET", `/projects/${p.id}/runs`);
  const rows = runs.runs.map(r => {
    const kind = isBaselineRun(r) ? pill("原始测评", "brand") : pill("自动优化", "");
    const kept = (r.candidates || []).filter(c => c.decision === "kept").length;
    return `<tr><td class="small"><a href="#/proj/${p.id}/run/${r.id}">${esc(r.id)}</a></td>
    <td>${kind}</td><td>${pill(runStateLabel(r.state), r.state === "completed" ? "ok" : r.state === "failed" ? "bad" : "warn")}</td>
    <td class="small">${esc(runStopLabel(r.stop_reason))}</td>
    <td class="small">${kept} 保留 / ${(r.candidates || []).length} 候选</td>
    <td class="small">${esc(fmtTime(r.created_at))}</td></tr>`;
  }).join("");
  return `<div class="card"><b>历次优化</b>
    <p class="muted small">离开页面不取消运行；每次运行有独立地址，刷新后仍是对应运行（§12）。</p>
    ${runs.runs.length
      ? `<table><tr><th>运行</th><th>类型</th><th>状态</th><th>停止原因</th><th>候选</th><th>时间</th></tr>${rows}</table>`
      : `<div class="empty">还没有运行过。<br>
         建议顺序：先去<a href="#/proj/${p.id}/baseline">原始测评</a>看看现状差在哪，再回来启动自动优化。<br>
         <a href="#/proj/${p.id}/baseline"><button class="grey" style="margin-top:8px">去做原始测评</button></a></div>`}</div>`;
}
PAGES.runs = async (p) => renderRunList(p);

/* ---------------- 运行详情（独立可恢复路由 §8.4/§12） ---------------- */
let _pollTimer = null;
let _stateTimer = null;
function fmtLedger(b) {
  const s = (b.spent || {}), r = (b.reserved || {});
  const sum = o => Object.values(o).reduce((a, x) => a + (Number(x) || 0), 0);
  return `已用 ${sum(s).toLocaleString()}（搜索 ${Number(s.search || 0).toLocaleString()} + 验收 ${Number(s.acceptance || 0).toLocaleString()}）· ` +
    `在途预留 ${sum(r).toLocaleString()}（额度内预估，完成后结算）`;
}
async function pageRunDetail(rid) {
  clearInterval(_pollTimer); clearInterval(_stateTimer);
  const [r] = await Promise.all([api("GET", `/runs/${rid}`)]);
  const live = ["running", "waiting_human", "paused_budget", "stopping", "queued"].includes(r.state);
  const kept = (r.candidates || []).filter(c => c.decision === "kept");
  const canLock = r.state === "completed" && !r.locked_candidate;
  const candRows = (r.candidates || []).map(c => `
    <tr><td class="small">${esc(c.candidate_id)}</td>
    <td>${pill(c.decision === "kept" ? "保留" : c.decision === "retained_alt" ? "备选" : "淘汰",
        c.decision === "kept" ? "ok" : c.decision === "retained_alt" ? "brand" : "")}</td>
    <td>${c.score != null ? c.score.toFixed(3) : ""}</td>
    <td>${c.usable_rate != null ? (c.usable_rate * 100).toFixed(0) + "%" : ""}</td>
    <td class="small">${c.regressions || 0} 回退 / ${c.severe || 0} 严重 / ${c.fixed_problems ? c.fixed_problems.length : 0} 修复</td>
    <td class="small">${c.length || ""} 字</td>
    <td class="small" style="white-space:normal">${esc(c.rationale || "")}</td>
    <td>${canLock && (c.decision === "kept" || c.decision === "retained_alt")
      ? `<button onclick="lockCand('${r.id}','${c.candidate_id}')">锁定为待验证</button>` : ""}</td></tr>`).join("");
  const roundCards = (r.rounds || []).map(rd => `
    <div class="card round">
      <div class="flex" style="align-items:baseline">
        <div style="flex:1"><b>第 ${rd.round_no} 轮</b>
          ${pill(rd.status === "scored" ? "已测评" : rd.status === "rewrite_failed" ? "改写失败" : "无修改",
            rd.status === "scored" ? "ok" : "warn")}</div>
        <div class="small muted">${esc(fmtTime(rd.created_at))}</div>
      </div>
      ${rd.hypothesis ? `<div class="small"><b>修改假设：</b>${esc(rd.hypothesis)}</div>` : ""}
      ${rd.rationale ? `<div class="small ${rd.status === "rewrite_failed" ? "" : "muted"}"><b>保留决定：</b>${esc(rd.rationale)}</div>` : ""}
      ${rd.status === "scored" ? `<div class="small muted">平均分 ${rd.prev_score != null ? Number(rd.prev_score).toFixed(3) : "-"} → ${rd.score != null ? Number(rd.score).toFixed(3) : "-"}；
        可用率 ${(Number(rd.usable_rate) * 100 || 0).toFixed(0)}%；长度 ${rd.length_chars} 字</div>` : ""}
      ${rd.next_direction ? `<div class="small muted">下一轮方向：${esc(rd.next_direction)}</div>` : ""}
    </div>`).join("");
  setMain(`
  <div class="flex" style="align-items:baseline"><div style="flex:1">
    <h1>运行 ${esc(r.id)}</h1>
    <p class="sub">${pill(runStateLabel(r.state), r.state === "completed" ? "ok" : r.state === "failed" ? "bad" : "warn")}
    停止原因：${esc(runStopLabel(r.stop_reason))} ·
    ${isBaselineRun(r) ? "原始测评" : "自动优化"} · 阶段 ${esc(r.stage || "-")}</p></div>
    <div style="flex:0"><a class="small" href="#/proj/${state.pid}/optimize">← 返回运行列表</a></div></div>
  ${r.state === "running" ? `<div class="card"><p class="muted small">正在优化：第 ${r.round_no + 1} 轮。
    正在检查：原问题是否减少；其他判断是否退步；评分是否仍符合标准。未完成全部轮数前不显示完成百分比。
    <b>运行结束后本页会自动刷新，无需手动操作。</b></p></div>` : ""}
  ${r.state === "waiting_human" ? `<div class="card focus"><b>等待人工参与</b>
    <p class="muted small">本轮已完成，等待指定评价。离开页面不影响运行；确认评价后点击继续。</p>
    <button onclick="continueRun('${r.id}')">继续优化 →</button></div>` : ""}
  ${r.state === "paused_budget" ? `<div class="card"><b>预算暂停</b>
    <p class="muted small">搜索预算已用完；独立验证的预留额度没有被占用。已完成的输出全部保留，可调整预算后恢复运行。</p>
    <button onclick="resumeRun('${r.id}')">恢复运行</button></div>` : ""}
  <div class="card">
    <b>基线与候选对比（不只比较平均分）</b>
    <div class="stat-hero">
      <div class="stat"><div class="k">基线平均分</div>
        <div class="v">${r.baseline_score != null && !(live && !Number(r.baseline_score))
          ? Number(r.baseline_score).toFixed(3) : (live ? "测评中…" : "-")}</div>
        <div class="sub2">${live ? "完成后作为所有候选的比较起点" : "所有候选都与它比较"}</div></div>
      <div class="stat ${r.locked_candidate ? "" : "attention"}"><div class="k">待验证版本
        <span class="head-pill ${r.locked_candidate ? "ok" : ""}">${r.locked_candidate ? "已锁定" : "未锁定"}</span></div>
        <div class="v">${r.locked_candidate ? esc(r.locked_candidate) : canLock ? "尚未选择" : "未锁定"}</div>
        <div class="sub2">${r.locked_candidate ? "可去「验证与使用」做最终检验" : canLock ? "在下方锁定一个候选，或保留原版" : "完成运行后可锁定"}</div></div>
      <div class="stat"><div class="k">账本用量 <span class="head-pill ok">本地计费</span></div>
        <div class="v small" style="font-size:13px;white-space:normal">${fmtLedger(r.budget)}</div></div>
    </div>
    ${(r.candidates || []).length ? `<table class="cand-table" style="margin-top:8px"><tr><th>候选</th><th>决定</th><th>平均分</th><th>可用率</th><th>底线检查</th><th>长度</th><th>依据</th><th></th></tr>${candRows}</table>`
      : `<p class="muted small">尚无候选${r.state === "completed" ? "：本轮没有产生值得保留的修改，保留基线为合法结果。" : ""}。</p>`}
    <div style="margin-top:8px">
      ${canLock ? `<button class="grey" onclick="lockCand('${r.id}','baseline')">不采用候选，保留原版</button>` : ""}
      ${r.locked_candidate && r.locked_candidate !== "baseline" ? `<a href="#/proj/${state.pid}/verify"><button>去验证与使用 →</button></a>` : ""}
      ${r.locked_candidate === "baseline" ? `<span class="muted small">已保留原版。如需对新版本做验证，可在“验证与使用”页选择。</span>` : ""}
      ${r.state !== "completed" && r.state !== "cancelled" && r.state !== "failed" ?
        `<button class="danger" onclick="cancelRun('${r.id}',${r.revision})">停止优化（保留已完成结果）</button>` : ""}
      <details class="small"><summary>事件流与日志（默认折叠）</summary><pre id="ev-out" class="small">加载中…</pre></details>
    </div>
  </div>
  ${roundCards || ""}`);
  pollEvents(rid, 0);
  if (r.state === "running") _pollTimer = setInterval(() => pollEvents(rid, window._evCursor || 0), 2500);
  if (live) {
    // 运行态下轮询运行本身：状态一变（完成/失败/暂停/等待人工）立即整页刷新，避免用户面对滞后页面
    let lastState = r.state;
    _stateTimer = setInterval(async () => {
      try {
        const cur = await api("GET", `/runs/${rid}`);
        if (cur.state !== lastState) {
          clearInterval(_stateTimer); clearInterval(_pollTimer);
          pageRunDetail(rid);
        }
      } catch (e) { /* 静默重试 */ }
    }, 2500);
  }
}
async function pollEvents(rid, cursor) {
  try {
    const r = await api("GET", `/runs/${rid}/events?cursor=${cursor}`);
    window._evCursor = r.events.length ? r.events[r.events.length - 1].seq : cursor;
    const el = document.getElementById("ev-out");
    if (el && r.events.length) {
      if (el.textContent.startsWith("加载中")) el.textContent = "";
      el.textContent += r.events.map(e => `[${e.seq}] ${e.type} ${JSON.stringify(e.payload)}`).join("\n") + "\n";
    }
  } catch (e) { /* 轮询失败静默重试 */ }
}
async function continueRun(rid) {
  await api("POST", `/runs/${rid}/continue`); toast("已继续优化");
  pageRunDetail(rid);
}
async function resumeRun(rid) { await api("POST", `/runs/${rid}/resume`); pageRunDetail(rid); }
async function lockCand(rid, cid) {
  try {
    await api("POST", `/runs/${rid}/lock`, { candidate_id: cid });
    toast(cid === "baseline" ? "已保留原版。" : "已锁定待验证版本——最后一步：用“考题”做独立验证。");
    pageRunDetail(rid);
  } catch (e) { toast(errText(e), true); }
}
async function cancelRun(rid, revision) {
  try { await api("POST", `/runs/${rid}/cancel`, { revision });
    toast("已请求停止：停止派发新请求，在途结果仍入账；已完成输出保留"); pageRunDetail(rid); }
  catch (e) { toast(errText(e), true); }
}

/* ---------------- 第五步：验证与使用（§9） ---------------- */
function decisionPill(d) {
  const map = { verified_improvement: ["验证有效", "ok"], no_improvement: ["未见提升", "warn"],
    regression: ["存在退步", "bad"], inconclusive: ["证据不足（尚未证实）", "warn"],
    evaluation_invalid: ["评价无效", "bad"] };
  const [label, cls] = map[d] || [d, ""];
  return pill(label, cls);
}
PAGES.verify = async (p) => {
  const prog = await getProgress(p.id);
  const [runs, reps] = await Promise.all([
    api("GET", `/projects/${p.id}/runs`), api("GET", `/projects/${p.id}/reports`)]);
  const done = runs.runs.filter(r => r.state === "completed" && r.locked_candidate);
  const runOpts = done.map(r => `<option value="${r.id}">${esc(r.id)}（锁定：${esc(r.locked_candidate)}）</option>`).join("");
  const repRows = reps.reports.map(r => `
    <tr><td class="small">${esc(r.id)}</td><td>${decisionPill(r.decision)}</td>
    <td>${(r.stats.diff * 100).toFixed(1)}pp</td>
    <td class="small">考题 ${r.stats.group_n} 组（未知 ${r.stats.unknown}）</td>
    <td class="small">${esc(fmtTime(r.created_at))}</td></tr>`).join("");
  return `
  ${stepBar(prog, p.id)}
  ${flowIntro("最终检验：用优化期间系统碰不到的“考题”（" + "封存测试集）测试，防止只是背会了练习题。",
    "先在运行详情里锁定一个待验证版本（或选择保留原版），然后回到这里点“发起独立验证”。",
    "得到明确结论：验证有效 / 未见提升 / 存在退步 / 证据不足——以及完整的效果报告和新提示词。")}
  <div class="card">
    <b>为什么要独立验证</b>
    <p class="muted small">检查修改是否不仅修好了已经看过的案例，还适用于未参与优化的新情况。
    固定候选、评价规则和模型配置后，对原始版与候选版进行可比测试。
    ${reps.reports.length ? "" : pill("尚未独立验证", "warn")}</p>
    <div class="flex">
      <div><label>已完成并锁定候选的运行</label><select id="acc-run">${runOpts}</select></div>
      <div style="flex:0"><label> </label><button onclick="acceptRun()"
        title="解封考题并做最终对照测试">发起独立验证（解封测试集）</button></div>
    </div>
    <div class="tip"><b>发起后会怎样：</b>考题（封存测试集）解封一次性使用，之后不能再作为独立证明——
      所以请在确认候选版本满意后再发起。结论只有五种，全部如实呈现：</div>
    <div class="legend-row">
      <div class="legend-chip"><span class="dot2" style="background:var(--ok)"></span><span><b>验证有效</b><small>改善有独立证据支持，可采用</small></span></div>
      <div class="legend-chip"><span class="dot2" style="background:var(--warn)"></span><span><b>未见提升</b><small>保留原版是正常结果</small></span></div>
      <div class="legend-chip"><span class="dot2" style="background:var(--bad)"></span><span><b>存在退步</b><small>不要采用，回炉重新优化</small></span></div>
      <div class="legend-chip"><span class="dot2" style="background:var(--faint)"></span><span><b>证据不足</b><small>通常是考题太少，不代表变差</small></span></div>
      <div class="legend-chip"><span class="dot2" style="background:var(--muted)"></span><span><b>评价无效</b><small>评分过程出错，结论不可信</small></span></div>
    </div>
    ${done.length ? "" : `<p class="small muted" style="margin-top:4px">还没有可验证的运行：先在「自动优化」里完成一次运行，并在运行详情中锁定待验证版本（或保留原版）。</p>`}
  </div>
  <div class="card">
    <b>验证报告</b>
    ${reps.reports.length ? `<table><tr><th>报告</th><th>结论</th><th>配对差异</th><th>规模</th><th>时间</th></tr>${repRows}</table>`
      : `<p class="muted small">尚无报告。注意：优化样例上的改善不构成独立证明。</p>`}
  </div>
  ${reps.reports.length ? await renderReportDetail(reps.reports[0].id) : ""}
  <div class="card">
    <b>使用与继续优化</b>
    <p class="muted small">复制文本不等于验证通过；只有结论为“验证有效”的报告才支持正式采用（发布记录会绑定该报告）。
    没有独立考题时会明确标记“尚未独立验证”。最终验证反馈一旦用于继续改写，该批材料不再作为下一轮的独立证明。</p>
  </div>
  <div class="action-bar">
    <a href="#/proj/${p.id}/optimize"><button class="grey">← 回到自动优化</button></a>
    <span class="grow"></span>
    <a href="#/proj/${p.id}/usage"><button>发布 / 回滚 / 反馈 →</button></a>
  </div>`;
};
async function renderReportDetail(repId) {
  const r = await api("GET", `/reports/${repId}`);
  const s = r.stats;
  const psStats = s.problem_stats || {};
  const es = s.evidence_scope || {};
  const abcd = psStats.abcd || {};
  // 通俗解读：先给一句话结论与建议，再给细节
  const nextAdvice = {
    verified_improvement: `改善有独立证据支持，可以采用候选版本投入实际使用（见第 6 节复制）。
      注意“验证有效”基于本批考题范围；换人群或换题型时建议再验证。`,
    no_improvement: `没有发现可靠改善——这是正常结果，保留原版即可。候选版本仍保留在「提示词版本」中，
      想继续尝试可回第三/四步补充专家意见后再次优化。`,
    regression: `候选版本存在退步，不要采用。建议回第四步查看淘汰候选的依据，或补充专家意见后重新优化。`,
    inconclusive: `证据不足通常是因为考题（封存测试集）太少——本批考题已被这次验证消耗，不能再作为独立证明。
      建议：准备更多考题案例（不同来源、覆盖真实场景），优化出新候选后再次独立验证。`,
    evaluation_invalid: `评价过程本身出了问题（如评分失败过多），结论不可信。请检查评价标准与模型设置后重试。`,
  }[r.decision] || "";
  const nextBox = nextAdvice ? `<div class="next-step-box"><b>建议下一步：</b>${nextAdvice}</div>` : "";
  let promptBlock = "";
  try {
    const cand = await api("GET", `/prompts/${r.candidate_ref}`);
    const base = await api("GET", `/prompts/${r.baseline_ref}`);
    promptBlock = `
    <div class="grid2">
      <div><b>原始版（v${base.version_no}，${base.length || String(base.body).length} 字）</b><div class="out-text small">${esc(base.body)}</div></div>
      <div><b>候选版（v${cand.version_no}，${cand.length || String(cand.body).length} 字）</b><div class="out-text small">${esc(cand.body)}</div></div>
    </div>
    <button class="grey" onclick="copyPrompt('${r.candidate_ref}')">复制候选提示词</button>
    <button class="grey" onclick="exportReport('${r.id}')">导出报告（JSON）</button>
    <span class="muted small">复制文本≠验证通过：只有结论为“验证有效”时才建议正式采用。</span>`;
  } catch (e) { promptBlock = `<p class="muted small">（提示词明细不可用）</p>`; }
  const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  return `
  <div class="card">
    <b>结果报告 ${esc(r.id)}</b> ${decisionPill(r.decision)}
    <div class="tip" style="margin-top:8px"><b>怎么看这份报告：</b>
      先看第 1 节的结论和建议；第 2、3 节回答“你最关心的问题解决了吗、有没有改出新问题”；
      第 4 节是独立考题上的对照数据；第 6 节可直接复制新提示词。统计术语可悬停查看，也可查「名词解释」。</div>
    ${nextBox}
    <div class="report-section"><h2>1. 结论</h2>
      <div class="stat-grid">
        ${stat("结论", decisionPill(r.decision))}
        ${stat("原始版可用率", (s.baseline_usable_rate * 100).toFixed(1) + "%")}
        ${stat("候选版可用率", (s.candidate_usable_rate * 100).toFixed(1) + "%")}
        ${stat("差异", (s.diff >= 0 ? "+" : "") + (s.diff * 100).toFixed(1) + "pp")}
      </div>
      <div class="small muted">主指标：${esc(s.primary_metric || "-")}（“可用”指按你确认的评价标准达到可直接使用的水平）</div>
    </div>
    <div class="report-section"><h2>2. 专家原问题的解决情况</h2>
      <div class="small">${psStats.total_registered ? `
      问题项 ${psStats.total_registered} 个（涉及案例 ${psStats.rated_cases} 个）：
      完全解决 ${psStats.resolution.resolved}、部分解决 ${psStats.resolution.partial}、
      未解决 ${psStats.resolution.unresolved}、无法判断 ${psStats.resolution.unknown}${psStats.resolution.already_ok ? `、基线已正常 ${psStats.resolution.already_ok}` : ""}
      <div class="muted">问题按“条”与案例按“个”分开统计，不混用。</div>`
      : esc(cleanNote(psStats.note) || "本项目未登记专家原问题，按通用评价标准评判；可回第二步补充专家意见获得针对性结论。")}</div>
    </div>
    <div class="report-section"><h2>3. 新增问题与原有能力退步</h2>
      <div class="stat-grid">
        ${stat("严重错误（原始版）", s.severe_baseline)}
        ${stat("严重错误（候选版）", s.severe_candidate)}
        ${s.severe_upper_bound_95 != null ? stat("候选零严重错误观察", `<small>95%置信上界 ${(s.severe_upper_bound_95 * 100).toFixed(2)}%</small>`) : ""}
      </div>
      <div class="small muted">ABCD 评级分布：A 完全解决 ${abcd.A ?? 0} / B 部分解决 ${abcd.B ?? 0} / C 未解决 ${abcd.C ?? 0} / D 出现新问题 ${abcd.D ?? 0} /
      无法判断 ${abcd.cannot_judge ?? 0} / 未登记 ${abcd.not_rated ?? 0}。
      ${s.severe_upper_bound_95 != null ? "“零严重错误观察”不等于真实零风险，上界才是可靠表述。" : ""}</div>
    </div>
    <div class="report-section"><h2>4. 独立验证结果（考题上的对照）</h2>
      <div class="stat-grid">
        ${stat("考题组数", s.group_n)}
        ${stat("仅候选可用（修复）", s.fix)}
        ${stat("仅原始版可用（退步）", s.regress)}
        ${stat("两者都可用", s.both)}
        ${stat("两者都不可用", s.neither)}
      </div>
      <details class="small"><summary>统计明细（供核对，可折叠）</summary>
        <div class="small muted">精确 McNemar 双侧 p = ${s.mcnemar_p != null ? Number(s.mcnemar_p).toFixed(6) : "-"}；
        配对 bootstrap 95% 区间 [${(s.bootstrap.ci_low * 100).toFixed(1)}pp, ${(s.bootstrap.ci_high * 100).toFixed(1)}pp]；
        缺失界限 ${(s.missing_bounds.low * 100).toFixed(1)}%–${(s.missing_bounds.high * 100).toFixed(1)}%
        （未知 ${s.missing_bounds.unknown_n} 条未删除，计入区间）。
        区间不跨 0 且观察提升达到门槛时才判“验证有效”。</div>
      </details>
    </div>
    <div class="report-section"><h2>5. 证据范围与统计口径</h2>
      <div class="small">${esc(es.optimization_sample?.role || "")}（${es.optimization_sample?.n ?? "-"} 条，不构成独立证明）；
      独立验证（未参与修改的案例）${es.independent?.n ?? "-"} 条，已消耗。
      <div class="muted">${esc(cleanNote(es.note))}</div></div>
    </div>
    <div class="report-section"><h2>6. 完整提示词与使用</h2>
      ${promptBlock}
    </div>
  </div>`;
}
async function copyPrompt(pvid) {
  const pv = await api("GET", `/prompts/${pvid}`);
  try { await navigator.clipboard.writeText(pv.body); toast("提示词已复制到剪贴板"); }
  catch (e) {
    const ta = document.createElement("textarea");
    ta.value = pv.body; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta);
    toast("提示词已复制");
  }
}
function exportReport(repId) {
  api("GET", `/reports/${repId}`).then(r => {
    const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `验证报告_${repId}.json`;
    a.click(); URL.revokeObjectURL(a.href);
    toast("报告已导出");
  });
}
async function acceptRun() {
  const rid = document.getElementById("acc-run").value;
  if (!rid) { toast("暂无可验收的运行：先完成优化并锁定候选", true); return; }
  try {
    const r = await api("POST", `/runs/${rid}/accept`);
    toast("独立验证完成：" + r.decision);
    route();
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 高级功能：案例与数据 ---------------- */
PAGES.data = async (p) => {
  advIntro("这里管理优化用的原始材料：导入案例、按来源分组、把案例分成练习（开发）与考题（封存测试）。改动集合后需重新冻结才会生效。")
  const items = await api("GET", `/projects/${p.id}/items?size=100`);
  const mans = await api("GET", `/projects/${p.id}/manifests`);
  const dsvs = await api("GET", `/projects/${p.id}/dataset_versions`);
  const splitName = { dev: "开发", select: "选择", sealed_test: "封存测试", unassigned: "未分配" };
  const splitCls = { dev: "ok", select: "brand", sealed_test: "bad", unassigned: "" };
  const rows = items.items.map(it => `
    <tr>
      <td class="small">${esc(it.case_id)}</td>
      <td class="small">${esc(it.source_group_id)}</td>
      <td><span class="pill dot ${splitCls[it.split] || ""}">${esc(splitName[it.split] || it.split)}</span></td>
      <td class="small">${esc(Object.entries(it.runtime_input).map(([k, v]) => `${k}=${v}`).join("｜").slice(0, 80))}</td>
      <td class="nowrap-actions">
        ${["dev", "select", "sealed_test"].map(sp =>
          `<button class="grey ${it.split === sp ? "current" : ""}"
            onclick="setSplit('${it.id}','${sp}')">${splitName[sp]}</button>`).join("")}
      </td>
    </tr>`).join("");
  return `
  ${advIntro("这里管理优化用的原始材料：导入案例、按来源分组、把案例分成练习（开发）与考题（封存测试）。改动集合后需重新冻结才会生效。")}
  <div class="card">
    <b>导入案例（JSONL / CSV）</b>
    <p class="muted small">预览校验会逐行指出问题，不会悄悄丢弃任何一行；同一批内容重复提交不会产生重复案例。</p>
    <div class="flex">
      <div style="flex:0 0 140px"><label>格式</label>
        <select id="imp-fmt"><option value="jsonl">JSONL</option><option value="csv">CSV（runtime:前缀）</option></select></div>
      <div style="flex:3"><label>内容</label>
        <textarea id="imp-content" placeholder='{"case_id":"c001","runtime_input":{...},"evaluation_only":{...}}'></textarea></div>
    </div>
    <div style="margin-top:8px">
      <button onclick="importPreview()">预览校验</button>
      <button class="ghost" onclick="importCommit()">提交导入</button>
      <span id="imp-result"></span>
    </div>
    <pre id="imp-preview" class="hidden"></pre>
  </div>
  <div class="card">
    <b>案例表</b> ${pill(`共 ${items.total} 条`, "")}
    <table><tr><th>案例ID</th><th>来源组</th><th>集合</th><th>输入</th><th>分配</th></tr>${rows}</table>
  </div>
  <div class="card">
    <b>分组切分冻结</b>
    ${(() => {
      const cnt = { dev: 0, select: 0, sealed_test: 0, unassigned: 0 };
      for (const it of items.items) cnt[it.split] = (cnt[it.split] || 0) + 1;
      const total = items.items.length || 1;
      const seg = [["dev", "开发集", "var(--ok)"], ["select", "选择集", "var(--brand)"],
        ["sealed_test", "封存考题", "var(--bad)"], ["unassigned", "未分配", "var(--faint)"]];
      return `<div class="ratio-line">当前配比：` + seg.map(([k, label, color]) =>
        cnt[k] ? `<span class="ratio-item"><i style="background:${color}"></i>${label}
          ${Math.round(cnt[k] / total * 100)}%（${cnt[k]}条）</span>` : "").join("") + `</div>`;
    })()}
    <p class="muted small" style="margin-top:6px">严谨性保障：冻结后集合标识将固化并写入数据快照，
    防止优化过程中产生数据泄漏与过拟合；考题仅在最終验收时使用。</p>
    <button onclick="freezeManifest()">冻结切分清单</button>
    <div class="small muted">已有清单：${mans.manifests.map(m =>
      `${esc(m.id)}（种子${m.seed}，${esc(m.state)}）`).join("；") || "暂无"}</div>
    <div class="small muted">数据版本：${dsvs.versions.map(v =>
      `v${v.version_no}`).join("，") || "暂无"}（每次导入/冻结都会形成可追溯的版本）</div>
  </div>`;
};
async function setSplit(itemId, split) {
  await api("POST", `/projects/${state.pid}/split`, { case_ids: [itemId], split });
  route();
}

/* ---------------- 高级功能：评价标准 ---------------- */
PAGES.rubric = async (p) => {
  advIntro("这里定义“怎样算好”。已发布版本不可修改，改动会生成新版本；标准变化会使已校准的评价器过期。")
  const rs = await api("GET", `/projects/${p.id}/rubrics`);
  const rows = rs.rubrics.map(r => `
    <tr><td>v${r.version_no}</td><td>${pill(r.status, r.status === "published" ? "ok" : "warn")}</td>
    <td class="small">${r.schema.dimensions.map(d => esc(d.name)).join("、")}</td>
    <td class="small">${esc(r.hash.slice(0, 12))}</td>
    <td>${r.status === "draft" ? `<button onclick="publishRubric('${r.id}')">发布</button>` : ""}
    <button class="grey" onclick='editRubric(${JSON.stringify(JSON.stringify(r.schema))})'>载入为新草稿</button></td></tr>`).join("");
  return `
  ${advIntro("这里定义“怎样算好”。已发布版本不可修改，改动会生成新版本；标准变化会使已校准的评价器过期。")}
  <div class="card"><b>评价标准</b>
    <p class="muted small">每个维度都要写全 0—3 分的含义（锚点）才能发布；标准一旦发布不可覆盖，修改会产生新版本，并使已校准的评价器过期。</p>
    <table><tr><th>版本</th><th>状态</th><th>维度</th><th>哈希</th><th>操作</th></tr>${rows}</table>
  </div>
  <div class="card">
    <b>草稿编辑</b>
    <button class="ghost" onclick="newRubricDraft()">从任务契约新建草稿</button>
    <label>标准 JSON（dimensions[].name/anchors{0,1,2,3}）</label>
    <textarea id="rubric-json" style="min-height:220px"></textarea>
    <div style="margin-top:8px"><button onclick="saveRubric()">保存草稿</button>
    <button onclick="saveRubric(true)">保存并发布</button></div>
  </div>`;
};
async function newRubricDraft() {
  const r = await api("POST", `/projects/${state.pid}/rubrics`);
  document.getElementById("rubric-json").value = JSON.stringify(r.schema, null, 2);
  toast("已按任务契约生成草稿 " + r.id);
}
let _editRubricId = "";
function editRubric(schemaJson) {
  _editRubricId = "";
  document.getElementById("rubric-json").value = JSON.stringify(JSON.parse(schemaJson), null, 2);
  toast("已载入该版本内容：请作为新草稿保存（已发布版本不可覆盖）");
}
async function saveRubric(publish) {
  let schema;
  try { schema = JSON.parse(document.getElementById("rubric-json").value); }
  catch (e) { toast("JSON 解析失败：" + e.message, true); return; }
  try {
    let r;
    if (_editRubricId) {
      r = await api("PUT", `/rubrics/${_editRubricId}`, schema); _editRubricId = "";
    } else {
      r = await api("POST", `/projects/${state.pid}/rubrics`);
      r = await api("PUT", `/rubrics/${r.id}`, schema);
    }
    if (publish) r = await api("POST", `/rubrics/${r.id}/publish`);
    toast(publish ? "已发布：" + r.id : "草稿已保存");
    route();
  } catch (e) { toast(errText(e), true); }
}
async function publishRubric(rid) {
  try { await api("POST", `/rubrics/${rid}/publish`); toast("已发布；旧评价器已标记stale"); route(); }
  catch (e) { toast(errText(e), true); }
}

/* ---------------- 高级功能：人工标注 ---------------- */
PAGES.annotation = async (p) => {
  advIntro("这里做人工盲评：两份输出匿名对比，支持判“相当 / 都不可用 / 无法判断”，用于校准评价或复核争议。")
  const outs = await api("GET", `/projects/${p.id}/outputs?limit=200`);
  const opts = outs.outputs.map(o =>
    `<option value="${o.id}">${o.id.slice(0, 14)}…（运行 ${esc((o.run_id || "").slice(0, 14))}，状态 ${esc(o.status)}）</option>`).join("");
  return `
  ${advIntro("这里做人工盲评：两份输出匿名对比，支持判“相当 / 都不可用 / 无法判断”，用于校准评价或复核争议。")}
  <div class="card"><b>创建匿名 A/B 对比</b>
    <p class="muted small">盲评视图不返回版本ID、名称、时间或评分，避免先入为主；除选择优胜方外，也可以判“相当 / 两边都不可用 / 无法判断”。</p>
    <div class="flex">
      <div><label>左侧输出</label><select id="pair-left">${opts}</select></div>
      <div><label>右侧输出</label><select id="pair-right">${opts}</select></div>
      <div style="flex:0"><button onclick="createPair()">创建对比</button></div>
    </div>
  </div>
  <div class="card"><b>盲评工作台</b>
    <div class="flex"><div style="flex:0 0 260px"><label>对比 public_id</label>
      <input id="blind-id" placeholder="pair_xxxxxxxx"></div>
      <div style="flex:0"><button class="ghost" onclick="loadBlind()">载入盲评</button></div></div>
    <div id="blind-area"></div>
  </div>`;
};
async function createPair() {
  const l = document.getElementById("pair-left").value, r = document.getElementById("pair-right").value;
  if (!l || !r) { toast("需要至少两个已存在的输出（先试运行或运行实验）", true); return; }
  const pr = await api("POST", `/projects/${state.pid}/pairs`,
    { left_output_id: l, right_output_id: r, purpose: "blind_ab" });
  document.getElementById("blind-id").value = pr.public_id;
  toast("对比已创建：" + pr.public_id);
  loadBlind();
}
async function loadBlind() {
  const pid = document.getElementById("blind-id").value.trim();
  if (!pid) { toast("请输入 public_id", true); return; }
  const b = await api("GET", `/pairs/${pid}/blind`);
  document.getElementById("blind-area").innerHTML = `
    <div class="grid2" style="margin-top:10px">
      <div><b>输出 A</b><div class="out-text">${esc(b.left.text)}</div></div>
      <div><b>输出 B</b><div class="out-text">${esc(b.right.text)}</div></div>
    </div>
    <label>偏好</label>
    <select id="blind-pref">
      <option value="A">A 更好</option><option value="B">B 更好</option><option value="tie">相当</option>
      <option value="both_unusable">两边都不可用</option><option value="unknown">无法判断</option>
    </select>
    <label>理由（简短）</label><input id="blind-reason">
    <div style="margin-top:8px"><button onclick="submitBlind('${pid}')">提交标注</button>
    <button class="grey" onclick="revealPair('${pid}')">提交后揭示真实版本</button></div>`;
}
async function submitBlind(pid) {
  const payload = { preference: document.getElementById("blind-pref").value,
    scores: {}, evidence: [], reason: document.getElementById("blind-reason").value };
  const r = await api("POST", `/pairs/${pid}/annotations`, payload);
  toast("标注已提交：" + r.id);
}
async function revealPair(pid) {
  const r = await api("POST", `/pairs/${pid}/reveal`);
  toast(`真实映射：左=${r.left_output_id.slice(0, 14)}…，右=${r.right_output_id.slice(0, 14)}…`);
}

/* ---------------- 高级功能：评价器校准 ---------------- */
PAGES.judge = async (p) => {
  advIntro("评价器按已发布的标准自动打分。校准用于检验它与人工判断的一致程度；构建与审计必须使用不同来源的案例。")
  const [rs, js, conns] = await Promise.all([
    api("GET", `/projects/${p.id}/rubrics`), api("GET", `/projects/${p.id}/judges`),
    api("GET", "/settings/connections")]);
  const rubOpts = rs.rubrics.filter(r => r.status === "published")
    .map(r => `<option value="${r.id}">v${r.version_no} ${r.id.slice(0, 10)}…</option>`).join("");
  const connOpts = conns.connections.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  const rows = js.judges.map(j => `
    <tr><td>v${j.version_no}</td><td>${pill(j.status, j.status === "audited" ? "ok" :
      j.status === "stale" ? "bad" : "warn")}</td>
    <td class="small">构建 ${j.build_refs.length} / 审计 ${j.audit_refs.length}</td>
    <td class="small">${j.metrics && j.metrics.audit ? `审计n=${j.metrics.audit.n}` : "未校准"}</td></tr>`).join("");
  return `
  ${advIntro("评价器按已发布的标准自动打分。校准用于检验它与人工判断的一致程度；构建与审计必须使用不同来源的案例。")}
  <div class="card"><b>创建评价器</b>
    <div class="flex">
      <div><label>评价标准（已发布）</label><select id="jdg-rubric">${rubOpts}</select></div>
      <div><label>评价模型连接</label><select id="jdg-conn">${connOpts}</select></div>
      <div style="flex:0"><button onclick="createJudge()">创建</button></div>
    </div>
  </div>
  <div class="card"><b>校准（构建集与审计集必须来自不同来源）</b>
    <label>构建集输出ID（每行一个，需人工核验gold）</label><textarea id="jdg-build" placeholder="out_..."></textarea>
    <label>审计集输出ID（独立来源，每行一个）</label><textarea id="jdg-audit"></textarea>
    <div style="margin-top:8px"><button onclick="calibrateJudge()">运行校准</button></div>
    <pre id="jdg-out" class="hidden"></pre>
  </div>
  <div class="card"><b>评价器列表</b>
    <table><tr><th>版本</th><th>状态</th><th>规模</th><th>指标</th></tr>${rows}</table>
  </div>`;
};
async function createJudge() {
  const r = await api("POST", `/projects/${state.pid}/judges`, {
    rubric_id: document.getElementById("jdg-rubric").value,
    model_cfg: { connection_id: document.getElementById("jdg-conn").value } });
  toast("评价器已创建：" + r.id);
  route();
}
function linesToIds(id) {
  return document.getElementById(id).value.split("\n").map(s => s.trim()).filter(Boolean);
}
async function calibrateJudge() {
  const js = await api("GET", `/projects/${state.pid}/judges`);
  const j = js.judges[js.judges.length - 1];
  try {
    const r = await api("POST", `/judges/${j.id}/calibrate`,
      { build_refs: linesToIds("jdg-build"), audit_refs: linesToIds("jdg-audit") });
    const out = document.getElementById("jdg-out");
    out.classList.remove("hidden");
    out.textContent = "状态：" + r.status + "\n" + JSON.stringify(r.metrics, null, 2);
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 高级功能：提示词库 ---------------- */
PAGES.prompts = async (p) => {
  advIntro("所有提示词版本都在这里：人工版本与优化候选并存，随时复制、对比或手动新建；运行与发布永远引用明确版本。")
  const ps = await api("GET", `/projects/${p.id}/prompts`);
  const rel = await api("GET", `/projects/${p.id}/releases`);
  const rows = ps.prompts.map(v => `
    <tr><td class="small">${esc(v.name)}</td><td>v${v.version_no}</td>
    <td>${pill(v.origin === "optimizer" ? "优化候选" : "人工", v.origin === "optimizer" ? "brand" : "")}</td>
    <td class="small">${esc(v.variables.join("、"))}</td>
    <td class="small">${v.length || v.body.length} 字</td>
    <td class="small">${esc(v.hash.slice(0, 10))}</td>
    <td>${rel.current && rel.current.prompt_version_id === v.id ? pill("当前使用", "ok") : ""}
    <button class="grey" onclick="copyPrompt('${v.id}')">复制</button></td>
    <td class="small" style="white-space:normal">${esc(v.hypothesis || "")}</td></tr>`).join("");
  const runtimeFields = p.contract.runtime_fields.map(f => f.name).join("、");
  return `
  ${advIntro("所有提示词版本都在这里：人工版本与优化候选并存，随时复制、对比或手动新建；运行与发布永远引用明确版本。")}
  <div class="card"><b>提示词版本库</b>
    <p class="muted small">运行时白名单变量：${esc(runtimeFields)}。每次运行都引用明确版本，不会受后续修改影响；新增版本不会改变“当前使用”的版本。</p>
    <table><tr><th>名称</th><th>版本</th><th>来源</th><th>变量</th><th>长度</th><th>哈希</th><th>指针</th><th>假设</th></tr>${rows}</table>
  </div>
  <div class="card"><b>新建版本</b>
    <label>名称</label><input id="pv-name" value="人工修改版">
    <label>正文（可用 {{变量}}）</label>
    <textarea id="pv-body"></textarea>
    <label>变量（逗号分隔，必须属于白名单）</label>
    <input id="pv-vars" value="${esc(runtimeFields)}">
    <label>冻结组件（JSON数组，可空）</label>
    <input id="pv-frozen" value='[]'>
    <div style="margin-top:8px"><button onclick="createPromptAdv()">创建新版本</button></div>
  </div>`;
};
async function createPromptAdv() {
  const frozen = document.getElementById("pv-frozen").value.trim();
  try {
    await api("POST", `/projects/${state.pid}/prompts`, {
      name: document.getElementById("pv-name").value.trim() || "默认提示词",
      body: document.getElementById("pv-body").value,
      variables: document.getElementById("pv-vars").value.split(",").map(s => s.trim()).filter(Boolean),
      frozen_segments: frozen ? JSON.parse(frozen) : [], params: {} });
    toast("新版本已创建（不改变当前使用指针）");
    route();
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 高级功能：试运行 ---------------- */
PAGES.playground = async (p) => {
  advIntro("单条试运行：选一个版本和一条案例立刻执行，用于快速检查格式与效果；每次调用都计入账本。")
  const [ps, items] = await Promise.all([
    api("GET", `/projects/${p.id}/prompts`), api("GET", `/projects/${p.id}/items?size=100`)]);
  const pvOpts = ps.prompts.map(v =>
    `<option value="${v.id}">${esc(v.name)} v${v.version_no}（${esc(originLabel(v.origin))}）</option>`).join("");
  const itOpts = items.items.map(i =>
    `<option value="${i.id}">${esc(i.case_id)}</option>`).join("");
  return `
  ${advIntro("单条试运行：选一个版本和一条案例立刻执行，用于快速检查格式与效果；每次调用都计入账本。")}
  <div class="card"><b>试运行（单条真实请求，全部计量）</b>
    <div class="flex">
      <div><label>提示词版本</label><select id="tr-pv">${pvOpts}</select></div>
      <div><label>案例</label><select id="tr-item">${itOpts}</select></div>
      <div style="flex:0"><button onclick="trial()">执行试运行</button></div>
    </div>
    <div id="tr-out"></div>
  </div>
  <div class="card"><b>版本对比</b>
    <div class="flex">
      <div><label>版本A</label><select id="df-a">${pvOpts}</select></div>
      <div><label>版本B</label><select id="df-b">${pvOpts}</select></div>
      <div style="flex:0"><button class="ghost" onclick="diffPrompt()">对比</button></div>
    </div>
    <pre id="df-out" class="hidden"></pre>
  </div>`;
};
async function trial() {
  const r = await api("POST", `/prompts/${document.getElementById("tr-pv").value}/trial`,
    { item_id: document.getElementById("tr-item").value });
  document.getElementById("tr-out").innerHTML = `
    <div style="margin-top:10px"><b>输出（${r.generation.status}）</b>
    <div class="out-text">${esc(r.generation.text || r.generation.error || "")}</div>
    ${r.evaluation ? `<b>评价</b><pre>${esc(JSON.stringify(r.evaluation, null, 2))}</pre>` : ""}</div>`;
}
async function diffPrompt() {
  const a = document.getElementById("df-a").value, b = document.getElementById("df-b").value;
  const d = await api("GET", `/prompts/${a}/diff?b_id=${encodeURIComponent(b)}`);
  const out = document.getElementById("df-out");
  out.classList.remove("hidden");
  out.textContent = `正文不同：${d.body_changed}；参数不同：${d.params_changed}；冻结段不同：${d.frozen_changed}\n\n` +
    `--- A 正文 ---\n${d.a_body}\n\n--- B 正文 ---\n${d.b_body}`;
}

/* ---------------- 高级功能：实验配置 ---------------- */
PAGES.experiment = async (p) => {
  advIntro("高级启动入口：完整配置快照、分阶段预算与批量模式。常规使用建议走左侧五步流程。")
  const [ps, mans, rs, js, conns] = await Promise.all([
    api("GET", `/projects/${p.id}/prompts`), api("GET", `/projects/${p.id}/manifests`),
    api("GET", `/projects/${p.id}/rubrics`), api("GET", `/projects/${p.id}/judges`),
    api("GET", "/settings/connections")]);
  const devItems = await api("GET", `/projects/${p.id}/items?split=dev&size=100`);
  const selItems = await api("GET", `/projects/${p.id}/items?split=select&size=100`);
  const pvOpts = ps.prompts.map(v => `<option value="${v.id}">${esc(v.name)} v${v.version_no}</option>`).join("");
  const manOpts = mans.manifests.map(m => `<option value="${m.id}">${esc(m.id)}</option>`).join("");
  const rubOpts = rs.rubrics.filter(r => r.status === "published")
    .map(r => `<option value="${r.id}">v${r.version_no}</option>`).join("");
  const judgeOpts = `<option value="">（探索模式：不用评价器）</option>` + js.judges
    .map(j => `<option value="${j.id}">v${j.version_no}（${esc(j.status)}）</option>`).join("");
  const connOpts = conns.connections.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  const devIds = devItems.items.map(i => i.id), selIds = selItems.items.map(i => i.id);
  window._devIds = devIds; window._selIds = selIds;
  return `
  ${advIntro("高级启动入口：完整配置快照、分阶段预算与批量模式。常规使用建议走左侧五步流程。")}
  <div class="card"><b>实验配置与启动（高级）</b>
    <p class="muted small">启动时会保存全部版本的快照（之后改配置不影响进行中的运行）；搜索与独立验证的预算分开记账；没有核实过价格时只能按 token 数设上限；同一键重复点击只会创建一次运行。</p>
    <div class="grid2">
      <div><label>基线提示词版本</label><select id="ex-pv">${pvOpts}</select></div>
      <div><label>切分清单</label><select id="ex-man">${manOpts}</select></div>
      <div><label>评价标准</label><select id="ex-rubric">${rubOpts}</select></div>
      <div><label>评价器（批量模式必须已审计）</label><select id="ex-judge">${judgeOpts}</select></div>
      <div><label>模式</label><select id="ex-mode"><option value="explore">explore 探索</option>
        <option value="batch">batch 批量</option></select></div>
      <div><label>生成/评价/优化连接</label><select id="ex-conn">${connOpts}</select></div>
      <div><label>优化轮数上限</label><input id="ex-cand" type="number" value="3"></div>
      <div><label>开发集抽样</label><input id="ex-sample" type="number" value="8"></div>
      <div><label>最小提升阈值</label><input id="ex-delta" type="number" step="0.01" value="0.05"></div>
      <div><label>连续无改善停止（轮）</label><input id="ex-stall" type="number" value="2"></div>
      <div><label>提示词长度上限（字）</label><input id="ex-len" type="number" value="4000"></div>
      <div><label>人工参与模式</label><select id="ex-human"><option value="">自动连续执行</option>
        <option value="1">每轮等待人工评价</option></select></div>
      <div><label>预算模式</label><select id="ex-bmode"><option value="token">token</option>
        <option value="money">money</option></select></div>
      <div><label>总额度</label><input id="ex-btotal" type="number" value="2000000"></div>
      <div><label>搜索额度</label><input id="ex-bsearch" type="number" value="1200000"></div>
      <div><label>验收预留</label><input id="ex-baccept" type="number" value="600000"></div>
    </div>
    <div class="small muted" style="margin-top:6px">开发集 ${devIds.length} 条 / 选择集 ${selIds.length} 条将按快照写入</div>
    <div style="margin-top:10px">
      <button class="ghost" onclick="validateRun()">校验并预估</button>
      <button onclick="startRunAdv()">启动实验</button>
    </div>
    <pre id="ex-out" class="hidden"></pre>
  </div>`;
};
function collectDraft() {
  const conn = document.getElementById("ex-conn").value;
  return {
    mode: document.getElementById("ex-mode").value,
    prompt: { baseline_id: document.getElementById("ex-pv").value },
    manifest_id: document.getElementById("ex-man").value,
    rubric_id: document.getElementById("ex-rubric").value,
    judge_id: document.getElementById("ex-judge").value || null,
    data: { dev_item_ids: window._devIds, select_item_ids: window._selIds },
    models: { generation: { connection_id: conn }, evaluation: { connection_id: conn },
              optimizer: { connection_id: conn } },
    optimization: { max_rounds: +document.getElementById("ex-cand").value,
      dev_sample_size: +document.getElementById("ex-sample").value,
      min_delta: +document.getElementById("ex-delta").value,
      stall_rounds: +document.getElementById("ex-stall").value,
      length_limit_chars: +document.getElementById("ex-len").value,
      human_in_loop: !!document.getElementById("ex-human").value },
    budget: { mode: document.getElementById("ex-bmode").value,
      total_limit: +document.getElementById("ex-btotal").value,
      search_limit: +document.getElementById("ex-bsearch").value,
      acceptance_limit: +document.getElementById("ex-baccept").value },
  };
}
async function validateRun() {
  try {
    const r = await api("POST", `/projects/${state.pid}/runs/validate`, collectDraft());
    document.getElementById("ex-out").classList.remove("hidden");
    document.getElementById("ex-out").textContent =
      "校验通过。\n" + JSON.stringify(r.estimate, null, 2);
  } catch (e) {
    document.getElementById("ex-out").classList.remove("hidden");
    document.getElementById("ex-out").textContent = errText(e);
  }
}
async function startRunAdv() {
  try {
    const r = await api("POST", `/projects/${state.pid}/runs`, collectDraft(),
      { "Idempotency-Key": "ui-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8) });
    toast("实验已启动：" + r.id);
    location.hash = `#/proj/${state.pid}/run/${r.id}`;
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 高级功能：验收报告 ---------------- */
PAGES.acceptance = async (p) => {
  advIntro("独立验收的原始入口；常规使用建议走第五步「验证与使用」，那里有更完整的报告解读。")
  const [runs, reps] = await Promise.all([
    api("GET", `/projects/${p.id}/runs`), api("GET", `/projects/${p.id}/reports`)]);
  const done = runs.runs.filter(r => r.state === "completed");
  const runOpts = done.map(r => `<option value="${r.id}">${esc(r.id)}（锁定：${esc(r.locked_candidate || "未锁定")}）</option>`).join("");
  const repRows = reps.reports.map(r => `
    <tr><td class="small">${esc(r.id)}</td><td>${decisionPill(r.decision)}</td>
    <td>${(r.stats.diff * 100).toFixed(1)}pp</td>
    <td class="small">n=${r.stats.group_n}（未知${r.stats.unknown}）</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted small" style="text-align:center">暂无报告——完成独立验证后自动生成</td></tr>`;
  return `
  ${advIntro("独立验收的原始入口；常规使用建议走第五步「验证与使用」，那里有更完整的报告解读。")}
  <div class="card"><b>发起独立验收</b>
    <p class="muted small">必须先在运行详情里锁定候选；解封后考题一次性消耗。</p>
    <div class="flex"><div><label>已完成的运行</label><select id="acc-run">${runOpts}</select></div>
    <div style="flex:0"><button onclick="acceptRun()">解封并验收</button></div></div>
  </div>
  <div class="card"><b>验收报告</b>
    ${reps.reports.length ? `<table><tr><th>报告</th><th>结论</th><th>配对差异</th><th>规模</th></tr>${repRows}</table>`
      : `<p class="muted small">暂无报告。</p>`}
  </div>`;
};

/* ---------------- 高级功能：使用与反馈 ---------------- */
PAGES.usage = async (p) => {
  advIntro("发布与回滚记录：正式采用必须绑定“验证有效”的报告；这里保留全部历史，回滚不删除任何记录。")
  const [rel, reps, ps, fb] = await Promise.all([
    api("GET", `/projects/${p.id}/releases`), api("GET", `/projects/${p.id}/reports`),
    api("GET", `/projects/${p.id}/prompts`), api("GET", `/projects/${p.id}/feedback`)]);
  const pvOpts = ps.prompts.map(v => `<option value="${v.id}">${esc(v.name)} v${v.version_no}</option>`).join("");
  const repOpts = `<option value="">（无——只能trial）</option>` + reps.reports
    .map(r => `<option value="${r.id}">${esc(r.id)}（${esc(r.decision)}）</option>`).join("");
  const hist = rel.history.map(h => `
    <tr><td class="small">${esc(h.id)}</td><td class="small">${esc(h.prompt_version_id)}</td>
    <td>${pill(h.status, h.status === "active" ? "ok" : h.status === "trial" ? "warn" : "")}</td>
    <td class="small">${esc(fmtTime(h.created_at))}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted small" style="text-align:center">暂无发布记录——完成验证后即可正式采用</td></tr>`;
  const fbRows = fb.feedback.map(f => `
    <tr><td class="small">${esc({direct:"直接采用",minor_edit:"轻微修改",major_edit:"实质修改",abandoned:"放弃"}[f.adoption] || f.adoption)}</td><td class="small">${esc(f.reason)}</td>
    <td class="small">${esc(f.status)}</td><td class="small">${esc(fmtTime(f.created_at))}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted small" style="text-align:center">暂无反馈——使用后欢迎回来记录实际效果</td></tr>`;
  return `
  ${advIntro("发布与回滚记录：正式采用必须绑定“验证有效”的报告；这里保留全部历史，回滚不删除任何记录。")}
  <div class="card"><b>当前使用版本</b>
    ${rel.current ? `<div class="kv">
      <div>发布ID</div><div class="small">${esc(rel.current.id)}</div>
      <div>提示词版本</div><div class="small">${esc(rel.current.prompt_version_id)}</div>
      <div>绑定报告</div><div class="small">${esc(rel.current.report_ref || "无")}</div></div>`
      : `<div class="muted">尚未正式发布任何版本。</div>`}
  </div>
  <div class="card"><b>发布</b>
    <div class="flex">
      <div><label>提示词版本</label><select id="rel-pv">${pvOpts}</select></div>
      <div><label>验收报告（正式采用必须绑定“验证有效”的报告）</label><select id="rel-rep">${repOpts}</select></div>
      <div style="flex:0"><label> </label>
        <button onclick="release('active')">正式采用</button>
        <button class="ghost" onclick="release('trial')">另存试用（不改指针）</button></div>
    </div>
  </div>
  <div class="card"><b>发布历史与回滚</b>
    <table><tr><th>发布</th><th>版本</th><th>状态</th><th>时间</th></tr>${hist}</table>
  </div>
  <div class="card"><b>使用反馈</b>
    <div class="flex">
      <div><label>针对发布ID</label><input id="fb-rel" value="${rel.current ? esc(rel.current.id) : ""}"></div>
      <div><label>采用状态</label><select id="fb-adopt"><option value="direct">直接采用</option>
        <option value="minor_edit">轻微修改</option><option value="major_edit">实质修改</option>
        <option value="abandoned">放弃</option></select></div>
      <div><label>原因</label><input id="fb-reason"></div>
      <div style="flex:0"><label> </label><button onclick="sendFeedback()">提交反馈</button></div>
    </div>
    <p class="muted small">使用反馈会进入待核验池，经人工确认后才可能成为新的练习案例，不会自动当作标准答案。</p>
    <table><tr><th>采用</th><th>原因</th><th>状态</th><th>时间</th></tr>${fbRows}</table>
  </div>`;
};
async function release(mode) {
  try {
    await api("POST", `/projects/${state.pid}/releases`, {
      prompt_version_id: document.getElementById("rel-pv").value,
      report_ref: document.getElementById("rel-rep").value, mode });
    toast(mode === "active" ? "已正式采用（指针已更新）" : "已保存为试用，不影响正式指针");
    route();
  } catch (e) { toast(errText(e), true); }
}
async function sendFeedback() {
  try {
    await api("POST", `/releases/${document.getElementById("fb-rel").value}/feedback`, {
      adoption: document.getElementById("fb-adopt").value,
      reason: document.getElementById("fb-reason").value });
    toast("反馈已记录（进入待核验池）");
    route();
  } catch (e) { toast(errText(e), true); }
}

/* ---------------- 模型设置（项目内外均可用，§4.1） ---------------- */
async function settingsHtml() {
  const [conns, prices] = await Promise.all([
    api("GET", "/settings/connections"), api("GET", "/settings/prices")]);
  const rows = conns.connections.map(c => `
    <tr><td class="small">${esc(c.id)}</td><td>${esc(c.name)}</td>
    <td>${pill(c.provider, c.provider === "mock" ? "warn" : "brand")}</td>
    <td class="small">${esc(c.model || "-")}</td>
    <td class="small">${c.api_key ? pill("密钥已配置", "ok") : pill("无密钥", "")}</td>
    <td><button class="grey" onclick="testConn('${c.id}')">测试</button></td></tr>`).join("");
  return `
  <div class="card"><b>模型连接（未创建项目时也可进入）</b>
    <p class="muted small">密钥只保存在本机数据库，界面只显示“已配置”，导出与日志不含密钥。内置模拟供应商可离线跑通全部流程（演示模式，不花钱）；
    真实使用请添加 OpenAI 兼容连接（智谱/DeepSeek/OpenAI等）。</p>
    <table><tr><th>ID</th><th>名称</th><th>类型</th><th>模型</th><th>密钥</th><th></th></tr>${rows}</table>
    <div class="grid2" style="margin-top:10px">
      <div><label>名称</label><input id="cn-name" placeholder="例如：智谱GLM"></div>
      <div><label>类型</label><select id="cn-provider"><option value="openai_compat">openai_compat</option>
        <option value="mock">mock</option></select></div>
      <div><label>Base URL（如 https://open.bigmodel.cn/api/paas/v4）</label><input id="cn-url"></div>
      <div><label>模型名</label><input id="cn-model" placeholder="glm-4-flash"></div>
      <div><label>API Key（只写不显示）</label><input id="cn-key" type="password"></div>
      <div style="flex:0;align-self:end"><button onclick="addConn()">保存连接</button></div>
    </div>
  </div>
  <div class="card"><b>价格表（按金额设预算上限时，需要每个在用模型都有已核实的单价）</b>
    <label>JSON：{"模型名": {"in_per_1k": 0.001, "out_per_1k": 0.002, "currency": "CNY"}}</label>
    <textarea id="price-json">${esc(JSON.stringify(prices.prices, null, 2))}</textarea>
    <div style="margin-top:8px"><button onclick="savePrices()">保存价格表</button></div>
  </div>`;
}
async function pageSettingsGlobal() {
  setMain(`<h1>模型设置</h1>
    <p class="sub">连接真实模型或使用内置演示供应商；密钥与价格只保存在本机。</p>` + await settingsHtml());
}
PAGES.settings = async (p) => settingsHtml();
async function addConn() {
  await api("PUT", "/settings/connections", {
    name: document.getElementById("cn-name").value,
    provider: document.getElementById("cn-provider").value,
    base_url: document.getElementById("cn-url").value,
    model: document.getElementById("cn-model").value,
    api_key: document.getElementById("cn-key").value });
  toast("连接已保存");
  route();
}
async function testConn(cid) {
  const r = await api("POST", `/settings/connections/${cid}/test`);
  toast(r.ok ? `连接正常，样例输出：${r.sample}` : `连接失败：${r.error}`, !r.ok);
}
async function savePrices() {
  try {
    const prices = JSON.parse(document.getElementById("price-json").value);
    await api("PUT", "/settings/prices", prices);
    toast("价格表已保存");
  } catch (e) { toast("JSON 解析失败：" + e.message, true); }
}

/* ---------------- 启动 ---------------- */
window.addEventListener("hashchange", route);
route();

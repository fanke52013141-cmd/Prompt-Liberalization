/* 提示词优化实验室 本地版界面。
   所有动态内容经 esc() 转义后渲染（TC051：恶意HTML不执行）。 */
"use strict";

const API = "/workflow-api/v1";
const state = { projects: [], pid: null, templates: {} };

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

/* ---------------- 导航 ---------------- */
const PROJECT_PAGES = [
  ["overview", "P02 总览与准备度"], ["data", "P03 案例与数据集"], ["rubric", "P04 评价标准"],
  ["annotation", "P05/P06 人工标注"], ["judge", "P07 评价器校准"], ["prompts", "P08 提示词库"],
  ["playground", "P09 编辑与试运行"], ["experiment", "P10 实验配置"], ["runs", "P11 实验运行"],
  ["acceptance", "P12 独立验收"], ["usage", "P13 使用与反馈"], ["settings", "P14 设置"],
];
function renderNav() {
  const h = location.hash || "#/projects";
  let html = `<a href="#/projects" class="${h === "#/projects" || h.startsWith("#/proj/") ? "active" : ""}">P01 项目列表</a>`;
  if (state.pid) {
    html += `<div class="group">当前项目</div>`;
    for (const [key, label] of PROJECT_PAGES) {
      const active = h === `#/proj/${state.pid}/${key}` ? "active" : "";
      html += `<a href="#/proj/${state.pid}/${key}" class="${active}">${esc(label)}</a>`;
    }
  }
  document.getElementById("nav").innerHTML = html;
}
function nav() { renderNav(); }

/* ---------------- 路由 ---------------- */
async function route() {
  const h = location.hash || "#/projects";
  const m = h.match(/^#\/proj\/([^/]+)\/([^/]+)$/);
  if (m) { state.pid = m[1]; await pageProject(m[2]); }
  else { state.pid = null; await pageProjects(); }
  renderNav();
  window.scrollTo(0, 0);
}
function setMain(html) { document.getElementById("main").innerHTML = html; }

/* ---------------- P01 项目列表 ---------------- */
async function pageProjects() {
  const [pr, tpl] = await Promise.all([api("GET", "/projects"), api("GET", "/templates")]);
  state.projects = pr.projects; state.templates = tpl;
  const cards = pr.projects.map(p => `
    <div class="card">
      <div class="flex">
        <div style="flex:2">
          <b>${esc(p.name)}</b> ${pill(esc(p.task_type), "brand")}
          ${p.status === "archived" ? pill("已归档", "warn") : pill("使用中", "ok")}
          <div class="muted small">${esc(p.description || "")} · 创建于 ${esc(p.created_at)}</div>
          <div class="muted small">任务单位：${esc(p.contract.evaluation_unit || "")}</div>
        </div>
        <div style="flex:0">
          <button onclick="openProject('${p.id}')">进入</button>
          ${p.status === "archived"
            ? `<button class="grey" onclick="unarchive('${p.id}')">取消归档</button>`
            : `<button class="grey" onclick="archive('${p.id}')">归档</button>`}
        </div>
      </div>
    </div>`).join("") || `<div class="card empty">还没有项目，先创建一个。</div>`;
  const opts = Object.entries(tpl).map(([k, t]) =>
    `<option value="${esc(k)}">${esc(t.label)}（${esc(k)}）</option>`).join("");
  setMain(`
    <h1>P01 项目列表与创建</h1>
    <p class="sub">每个项目绑定一种任务契约：不能把点评和标题混成一个评分项目。</p>
    ${cards}
    <div class="card">
      <b>新建项目</b>
      <div class="flex">
        <div><label>项目名称</label><input id="np-name" placeholder="例如：初中学员点评优化"></div>
        <div><label>任务模板</label><select id="np-task">${opts}</select></div>
      </div>
      <label>描述</label><input id="np-desc" placeholder="目标用户与业务目标（可空）">
      <div style="margin-top:10px"><button onclick="createProject()">创建项目</button>
      <span class="muted small">模板自带输入契约与评价标准草案（演示数据标识 is_demo）</span></div>
    </div>`);
}
async function createProject() {
  const name = document.getElementById("np-name").value.trim();
  if (!name) { toast("项目名称不能为空", true); return; }
  const p = await api("POST", "/projects", {
    name, description: document.getElementById("np-desc").value,
    task_type: document.getElementById("np-task").value });
  toast("项目已创建：" + p.id);
  location.hash = `#/proj/${p.id}/overview`;
}
function openProject(id) { location.hash = `#/proj/${id}/overview`; }
async function archive(id) { await api("POST", `/projects/${id}/archive`); toast("已归档：历史可读，新收费运行将被拒绝"); route(); }
async function unarchive(id) { await api("POST", `/projects/${id}/unarchive`); toast("已恢复"); route(); }

/* ---------------- P02 总览与准备度 ---------------- */
async function pageProject(page) {
  const p = await api("GET", `/projects/${state.pid}`);
  const head = `<h1>${esc(p.name)}</h1>
    <p class="sub">${pill(esc(p.task_type), "brand")} ${p.status === "archived" ? pill("已归档", "warn") : ""}
    ${esc(p.contract.evaluation_unit || "")} · 主指标：${esc(p.contract.primary_metric || "")}</p>`;
  const fn = PAGES[page] || PAGES.overview;
  const body = await fn(p);
  setMain(head + body);
}
const PAGES = {};
PAGES.overview = async (p) => {
  const r = await api("GET", `/projects/${p.id}/readiness`);
  const rows = r.checks.map(c => `
    <div class="check">
      <span class="dot" style="color:${c.ready ? "var(--ok)" : "var(--warn)"}">${c.ready ? "✓" : "○"}</span>
      <span style="flex:1">${esc(c.explain)}</span>
      <span class="muted small">补齐页面：${esc(c.page)}</span>
      <a href="#/proj/${p.id}/${pageKey(c.page)}" class="small">前往</a>
    </div>`).join("");
  const stateNote = r.state === "experiment_ready"
    ? pill("experiment_ready 可发起批量实验", "ok")
    : pill("当前状态：" + r.state, "warn");
  return `<div class="card"><b>P02 项目总览与引导工作台</b> ${stateNote}
    <p class="muted small">准备状态由依赖计算；缺少项逐条解释并跳转补齐页，不显示虚假就绪（TC004）。
    评价器未校准时可退回人工评价继续探索；探索结果标注"探索，未验证"，不允许伪造提升结论。</p>
    ${rows}</div>`;
};
function pageKey(pid) {
  return { P01: "overview", P02: "overview", P03: "data", P04: "rubric", P05: "annotation",
    P06: "annotation", P07: "judge", P08: "prompts", P09: "playground", P10: "experiment",
    P11: "runs", P12: "acceptance", P13: "usage", P14: "settings" }[pid] || "overview";
}

/* ---------------- P03 数据 ---------------- */
PAGES.data = async (p) => {
  const items = await api("GET", `/projects/${p.id}/items?size=100`);
  const mans = await api("GET", `/projects/${p.id}/manifests`);
  const dsvs = await api("GET", `/projects/${p.id}/dataset_versions`);
  const rows = items.items.map(it => `
    <tr>
      <td class="small">${esc(it.case_id)}</td>
      <td class="small">${esc(it.source_group_id)}</td>
      <td>${pill(it.split === "dev" ? "开发" : it.split === "select" ? "选择" :
        it.split === "sealed_test" ? "封存测试" : "未分配", it.split === "sealed_test" ? "bad" : "")}</td>
      <td class="small">${esc(Object.entries(it.runtime_input).map(([k, v]) => `${k}=${v}`).join("｜").slice(0, 80))}</td>
      <td>
        <button class="grey" onclick="setSplit('${it.id}','dev')">开发</button>
        <button class="grey" onclick="setSplit('${it.id}','select')">选择</button>
        <button class="grey" onclick="setSplit('${it.id}','sealed_test')">封存</button>
      </td>
    </tr>`).join("");
  return `
  <div class="card">
    <b>导入案例（JSONL / CSV）</b>
    <p class="muted small">预览校验逐行报错，不静默丢行；提交携带预览哈希，重复提交幂等（TC006/TC007）。
    evaluation_only 字段仅用于评价，绝不进入生成请求（BR01）。</p>
    <div class="flex">
      <div style="flex:0 0 140px"><label>格式</label>
        <select id="imp-fmt"><option value="jsonl">JSONL</option><option value="csv">CSV（runtime:前缀）</option></select></div>
      <div style="flex:3"><label>内容（每行一条 JSON，或 CSV 文本）</label>
        <textarea id="imp-content" placeholder='{"case_id":"c001","source_group_id":"g001","runtime_input":{...},"evaluation_only":{...}}'></textarea></div>
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
    <p class="muted small">同源分组跨开发/选择/封存将被阻止（TC008）；封存原文进入受限存储，
    普通列表与选择器无法读取（TC010）。冻结产生不可变数据版本（TC011）。</p>
    <button onclick="freezeManifest()">冻结切分清单</button>
    <div class="small muted">已有清单：${mans.manifests.map(m =>
      `${esc(m.id)}（种子${m.seed}，${esc(m.state)}）`).join("；") || "暂无"}</div>
    <div class="small muted">数据版本：${dsvs.versions.map(v =>
      `v${v.version_no} ${esc(v.id.slice(0, 12))}`).join("，") || "暂无"}</div>
  </div>`;
};
async function importPreview() {
  const fmt = document.getElementById("imp-fmt").value;
  const content = document.getElementById("imp-content").value;
  const b = await api("POST", `/projects/${state.pid}/imports/preview`, { fmt, content });
  window._lastBatch = b;
  document.getElementById("imp-preview").classList.remove("hidden");
  document.getElementById("imp-preview").textContent =
    `批次 ${b.id}\n总行数 ${b.total}，有效 ${b.valid}，错误 ${b.errors.length}\n` +
    (b.errors.map(e => `第${e.line}行 ${e.case_id || ""}：${(e.reasons || [e.reason]).join("；")}`).join("\n") || "无错误行");
  document.getElementById("imp-result").textContent =
    b.valid > 0 ? pill(`预览成功：有效 ${b.valid} 条，可提交`, "ok") : pill("无有效行", "bad");
}
async function importCommit() {
  if (!window._lastBatch) { toast("请先预览校验", true); return; }
  const r = await api("POST",
    `/projects/${state.pid}/imports/${window._lastBatch.id}/commit`, { exclude_case_ids: [] });
  toast(`导入完成：本批有效 ${r.valid} 条（重复提交幂等返回同一批次）`);
  route();
}
async function setSplit(itemId, split) {
  await api("POST", `/projects/${state.pid}/split`, { case_ids: [itemId], split });
  route();
}
async function freezeManifest() {
  try {
    const r = await api("POST", `/projects/${state.pid}/manifests/freeze`, { seed: 20260927 });
    toast(`已冻结：分组 ${r.groups} 个，封存测试 ${r.sealed_test_items} 条`);
    route();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- P04 评价标准 ---------------- */
PAGES.rubric = async (p) => {
  const rs = await api("GET", `/projects/${p.id}/rubrics`);
  const rows = rs.rubrics.map(r => `
    <tr><td>v${r.version_no}</td><td>${pill(r.status, r.status === "published" ? "ok" : "warn")}</td>
    <td class="small">${r.schema.dimensions.map(d => esc(d.name)).join("、")}</td>
    <td class="small">${esc(r.hash.slice(0, 12))}</td>
    <td>${r.status === "draft" ? `<button onclick="publishRubric('${r.id}')">发布</button>` : ""}
    <button class="grey" onclick='editRubric(${JSON.stringify(JSON.stringify(r.schema))})'>编辑草稿</button></td></tr>`).join("");
  return `
  <div class="card"><b>评价标准与标签库</b>
    <p class="muted small">维度锚点 0-3 齐全才能发布（TC012）；发布后产生新版本，
    旧报告只读，不与新标准直接比较（TC013）。标准变化会使评价器进入 stale（TC024）。</p>
    <table><tr><th>版本</th><th>状态</th><th>维度</th><th>哈希</th><th>操作</th></tr>${rows}</table>
  </div>
  <div class="card">
    <b>草稿编辑</b>
    <button class="ghost" onclick="newRubricDraft()">从任务模板新建草稿</button>
    <label>标准 JSON（dimensions[].name/anchors{0,1,2,3}）</label>
    <textarea id="rubric-json" style="min-height:220px"></textarea>
    <div style="margin-top:8px"><button onclick="saveRubric()">保存草稿</button>
    <button onclick="saveRubric(true)">保存并发布</button></div>
  </div>`;
};
async function newRubricDraft() {
  const r = await api("POST", `/projects/${state.pid}/rubrics`);
  document.getElementById("rubric-json").value = JSON.stringify(r.schema, null, 2);
  toast("已按任务模板生成草稿 " + r.id);
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
      // 草稿已创建，直接写入其内容再按需发布
      r = await api("PUT", `/rubrics/${r.id}`, schema);
    }
    if (publish) r = await api("POST", `/rubrics/${r.id}/publish`);
    toast(publish ? "已发布：" + r.id : "草稿已保存");
    route();
  } catch (e) {
    const fe = e.body && e.body.field_errors
      ? "\n" + Object.entries(e.body.field_errors).map(([k, v]) => `${k}: ${v}`).join("\n") : "";
    toast(e.message + fe, true);
  }
}
async function publishRubric(rid) {
  try { await api("POST", `/rubrics/${rid}/publish`); toast("已发布；旧评价器已标记stale"); route(); }
  catch (e) { toast(e.message, true); }
}

/* ---------------- P05/P06 标注 ---------------- */
PAGES.annotation = async (p) => {
  const outs = await api("GET", `/projects/${p.id}/outputs?limit=200`);
  const opts = outs.outputs.map(o =>
    `<option value="${o.id}">${o.id.slice(0, 14)}…（运行 ${esc((o.run_id || "").slice(0, 14))}，状态 ${esc(o.status)}）</option>`).join("");
  return `
  <div class="card"><b>创建匿名 A/B 对比（P05 队列入口）</b>
    <p class="muted small">盲评视图不返回版本ID、名称、时间或评分（TC018）；
    偏好支持 tie / both_unusable / unknown，不强选胜者（TC019）。</p>
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
  if (!l || !r) { toast("需要至少两个已存在的输出（先在 P09 试运行或 P11 运行实验）", true); return; }
  const pr = await api("POST", `/projects/${state.pid}/pairs`,
    { left_output_id: l, right_output_id: r, purpose: "blind_ab" });
  document.getElementById("blind-id").value = pr.public_id;
  toast("对比已创建（映射只存服务端）：" + pr.public_id);
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
    <button class="grey" onclick="revealPair('${pid}')">提交后揭示真实版本（负责人）</button></div>`;
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

/* ---------------- P07 评价器 ---------------- */
PAGES.judge = async (p) => {
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
    <td class="small">${j.metrics && j.metrics.audit ? `审计n=${j.metrics.audit.n}` : "未校准"}</td>
    <td><button class="grey" onclick="showJudge('${j.id}')">详情</button></td></tr>`).join("");
  return `
  <div class="card"><b>创建评价器</b>
    <div class="flex">
      <div><label>评价标准（已发布）</label><select id="jdg-rubric">${rubOpts}</select></div>
      <div><label>评价模型连接</label><select id="jdg-conn">${connOpts}</select></div>
      <div style="flex:0"><button onclick="createJudge()">创建</button></div>
    </div>
  </div>
  <div class="card"><b>校准（构建集/审计集必须来源隔离 TC021）</b>
    <label>构建集输出ID（每行一个，需人工核验gold）</label><textarea id="jdg-build" placeholder="out_..."></textarea>
    <label>审计集输出ID（独立来源，每行一个）</label><textarea id="jdg-audit"></textarea>
    <div style="margin-top:8px"><button onclick="calibrateJudge()">运行校准</button>
    <span class="muted small">模型预标注不能作为gold（TC020）；审计样本不足不能声称可靠（TC022）</span></div>
    <pre id="jdg-out" class="hidden"></pre>
  </div>
  <div class="card"><b>评价器列表</b>
    <table><tr><th>版本</th><th>状态</th><th>规模</th><th>指标</th><th></th></tr>${rows}</table>
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
  } catch (e) { toast(e.message, true); }
}
async function showJudge(jid) {
  const j = await api("GET", `/judges/${jid}`);
  const out = document.getElementById("jdg-out");
  out.classList.remove("hidden");
  out.textContent = JSON.stringify(j.metrics, null, 2);
}

/* ---------------- P08 提示词库 ---------------- */
PAGES.prompts = async (p) => {
  const ps = await api("GET", `/projects/${p.id}/prompts`);
  const rel = await api("GET", `/projects/${p.id}/releases`);
  const rows = ps.prompts.map(v => `
    <tr><td class="small">${esc(v.name)}</td><td>v${v.version_no}</td>
    <td>${pill(v.origin === "optimizer" ? "优化候选" : "人工", v.origin === "optimizer" ? "brand" : "")}</td>
    <td class="small">${esc(v.variables.join("、"))}</td>
    <td class="small">${esc(v.hash.slice(0, 10))}</td>
    <td>${rel.current && rel.current.prompt_version_id === v.id ? pill("当前使用", "ok") : ""}</td>
    <td class="small">${esc(v.hypothesis || "")}</td></tr>`).join("");
  const runtimeFields = p.contract.runtime_fields.map(f => f.name).join("、");
  return `
  <div class="card"><b>提示词版本库</b>
    <p class="muted small">运行时白名单变量：${esc(runtimeFields)}。
    实验引用明确版本ID，不引用 latest（TC031）；新增版本不改变当前使用指针（TC025）。</p>
    <table><tr><th>名称</th><th>版本</th><th>来源</th><th>变量</th><th>哈希</th><th>指针</th><th>假设</th></tr>${rows}</table>
  </div>
  <div class="card"><b>新建版本</b>
    <label>名称</label><input id="pv-name" value="学员点评提示词">
    <label>正文（可用 {{变量}}）</label>
    <textarea id="pv-body">你是一名教研老师。请根据题目、学员答案与学段，给出一份学员作答点评。</textarea>
    <label>变量（逗号分隔，必须属于白名单）</label>
    <input id="pv-vars" value="question,student_answer,grade_level">
    <label>冻结组件（JSON数组，可空）</label>
    <input id="pv-frozen" value='[{"name":"安全声明","text":"不得虚构学员错误。"}]'>
    <div style="margin-top:8px"><button onclick="createPrompt()">创建新版本</button></div>
  </div>`;
};
async function createPrompt() {
  const frozen = document.getElementById("pv-frozen").value.trim();
  try {
    await api("POST", `/projects/${state.pid}/prompts`, {
      name: document.getElementById("pv-name").value.trim() || "默认提示词",
      body: document.getElementById("pv-body").value,
      variables: document.getElementById("pv-vars").value.split(",").map(s => s.trim()).filter(Boolean),
      frozen_segments: frozen ? JSON.parse(frozen) : [], params: {} });
    toast("新版本已创建（不改变当前使用指针）");
    route();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- P09 编辑与试运行 ---------------- */
PAGES.playground = async (p) => {
  const [ps, items] = await Promise.all([
    api("GET", `/projects/${p.id}/prompts`), api("GET", `/projects/${p.id}/items?size=100`)]);
  const pvOpts = ps.prompts.map(v =>
    `<option value="${v.id}">${esc(v.name)} v${v.version_no}（${esc(v.origin)}）</option>`).join("");
  const itOpts = items.items.map(i =>
    `<option value="${i.id}">${esc(i.case_id)}</option>`).join("");
  return `
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
    ${r.evaluation ? `<b>模拟评价</b><pre>${esc(JSON.stringify(r.evaluation, null, 2))}</pre>` : ""}</div>`;
}
async function diffPrompt() {
  const a = document.getElementById("df-a").value, b = document.getElementById("df-b").value;
  const d = await api("GET", `/prompts/${a}/diff?b_id=${encodeURIComponent(b)}`);
  const out = document.getElementById("df-out");
  out.classList.remove("hidden");
  out.textContent = `正文不同：${d.body_changed}；参数不同：${d.params_changed}；冻结段不同：${d.frozen_changed}`;
}

/* ---------------- P10 实验配置 ---------------- */
PAGES.experiment = async (p) => {
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
  <div class="card"><b>实验配置与启动（P10）</b>
    <p class="muted small">配置快照固定全部版本；搜索与验收预算分列，搜索耗尽不占验收预留（TC033）；
    价格未知时只能用 token 上限模式（TC032）；双击幂等只建一次（TC035）。</p>
    <div class="grid2">
      <div><label>基线提示词版本</label><select id="ex-pv">${pvOpts}</select></div>
      <div><label>切分清单</label><select id="ex-man">${manOpts}</select></div>
      <div><label>评价标准</label><select id="ex-rubric">${rubOpts}</select></div>
      <div><label>评价器（批量模式必须已审计）</label><select id="ex-judge">${judgeOpts}</select></div>
      <div><label>模式</label><select id="ex-mode"><option value="explore">explore 探索</option>
        <option value="batch">batch 批量</option></select></div>
      <div><label>生成/评价/优化连接</label><select id="ex-conn">${connOpts}</select></div>
      <div><label>候选数</label><input id="ex-cand" type="number" value="2"></div>
      <div><label>开发集抽样</label><input id="ex-sample" type="number" value="8"></div>
      <div><label>最小提升阈值</label><input id="ex-delta" type="number" step="0.01" value="0.02"></div>
      <div><label>预算模式</label><select id="ex-bmode"><option value="token">token</option>
        <option value="money">money</option></select></div>
      <div><label>总额度</label><input id="ex-btotal" type="number" value="2000000"></div>
      <div><label>搜索额度</label><input id="ex-bsearch" type="number" value="1200000"></div>
      <div><label>验收预留</label><input id="ex-baccept" type="number" value="600000"></div>
    </div>
    <div class="small muted" style="margin-top:6px">开发集 ${devIds.length} 条 / 选择集 ${selIds.length} 条
    将按快照写入（引用明确版本）</div>
    <div style="margin-top:10px">
      <button class="ghost" onclick="validateRun()">校验并预估</button>
      <button onclick="startRun()">启动实验</button>
      <span id="ex-est" class="small muted"></span>
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
    optimization: { max_candidates: +document.getElementById("ex-cand").value,
      dev_sample_size: +document.getElementById("ex-sample").value,
      min_delta: +document.getElementById("ex-delta").value },
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
    const fe = e.body && e.body.field_errors
      ? "\n" + Object.entries(e.body.field_errors).map(([k, v]) => `${k}: ${v}`).join("\n") : "";
    document.getElementById("ex-out").classList.remove("hidden");
    document.getElementById("ex-out").textContent = e.message + fe;
  }
}
async function startRun() {
  try {
    const r = await api("POST", `/projects/${state.pid}/runs`, collectDraft(),
      { "Idempotency-Key": "ui-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8) });
    toast("实验已启动：" + r.id + "（幂等键保护双击）");
    location.hash = `#/proj/${state.pid}/runs`;
  } catch (e) { toast(e.message, true); }
}

/* ---------------- P11 实验运行 ---------------- */
PAGES.runs = async (p) => {
  const rs = await api("GET", `/projects/${p.id}/runs`);
  const rows = rs.runs.map(r => `
    <tr><td class="small">${esc(r.id)}</td>
    <td>${pill(r.state, r.state === "completed" ? "ok" : r.state === "failed" ? "bad" : "warn")}</td>
    <td class="small">${esc(r.stop_reason || "")}</td>
    <td class="small">${(r.candidates || []).length} 个候选</td>
    <td><a href="#/proj/${p.id}/runs" onclick="showRun('${r.id}')">详情</a></td></tr>`).join("");
  return `
  <div class="card"><b>运行列表</b>
    <table><tr><th>运行</th><th>状态</th><th>停止原因</th><th>候选</th><th></th></tr>${rows}</table>
  </div>
  <div id="run-detail"></div>`;
};
let _pollTimer = null;
async function showRun(rid) {
  clearInterval(_pollTimer);
  const r = await api("GET", `/runs/${rid}`);
  const led = await api("GET", `/runs/${rid}/ledger`);
  const cands = (r.candidates || []).map(c => `
    <tr><td class="small">${esc(c.candidate_id)}</td><td class="small">${esc(c.prompt_version_id)}</td>
    <td>${c.score != null ? c.score.toFixed(3) : ""}</td>
    <td>${c.usable_rate != null ? (c.usable_rate * 100).toFixed(0) + "%" : ""}</td>
    <td>${c.severe || 0}</td>
    <td><button onclick="lockCand('${rid}','${c.candidate_id}')" ${r.locked_candidate ? "disabled" : ""}>锁定</button></td></tr>`).join("");
  document.getElementById("run-detail").innerHTML = `
  <div class="card"><b>运行 ${esc(r.id)}</b> ${pill(r.state, r.state === "completed" ? "ok" : "warn")}
    <div class="kv" style="margin-top:8px">
      <div>快照哈希</div><div class="small">${esc(r.snapshot_hash.slice(0, 24))}…</div>
      <div>停止原因</div><div>${esc(r.stop_reason || "-")}</div>
      <div>锁定候选</div><div>${esc(r.locked_candidate || "未锁定")}</div>
      <div>预算（已用/在途）</div><div class="small">${esc(JSON.stringify(r.budget.spent))} / ${esc(JSON.stringify(r.budget.reserved))}</div>
      <div>账本对账</div><div class="small">确定 ${led.known_tokens} tok；未知在途 ${led.unknown_tokens} tok（sent_unknown单列，TC057）</div>
    </div>
    <div style="margin-top:8px">
      <button class="danger" onclick="cancelRun('${rid}',${r.revision})">取消（协作停止）</button>
      ${r.state === "paused_budget" ? `<button onclick="resumeRun('${rid}')">恢复运行</button>` : ""}
      <button class="ghost" onclick="pollEvents('${rid}',0)">刷新事件</button>
      ${r.state === "completed" && r.locked_candidate ? `<button onclick="location.hash='#/proj/${state.pid}/acceptance'">去独立验收 →</button>` : ""}
    </div>
    <h2>候选</h2>
    <table><tr><th>候选</th><th>版本</th><th>平均分</th><th>可用率</th><th>严重</th><th></th></tr>${cands}</table>
    <h2>事件流</h2><pre id="ev-out" class="small">点击"刷新事件"加载（断线可按游标恢复）</pre>
  </div>`;
  pollEvents(rid, 0);
  if (r.state === "running") _pollTimer = setInterval(() => pollEvents(rid, window._evCursor || 0), 2500);
}
async function pollEvents(rid, cursor) {
  const r = await api("GET", `/runs/${rid}/events?cursor=${cursor}`);
  window._evCursor = r.events.length ? r.events[r.events.length - 1].seq : cursor;
  const el = document.getElementById("ev-out");
  if (el && r.events.length) {
    el.textContent += r.events.map(e => `[${e.seq}] ${e.type} ${JSON.stringify(e.payload)}`).join("\n") + "\n";
  }
}
async function lockCand(rid, cid) {
  try { await api("POST", `/runs/${rid}/lock`, { candidate_id: cid }); toast("已锁定：" + cid); showRun(rid); }
  catch (e) { toast(e.message, true); }
}
async function cancelRun(rid, revision) {
  try { await api("POST", `/runs/${rid}/cancel`, { revision }); toast("已请求取消：停止派发，在途结果仍入账"); showRun(rid); }
  catch (e) { toast(e.message, true); }
}
async function resumeRun(rid) {
  await api("POST", `/runs/${rid}/resume`); toast("已恢复"); showRun(rid);
}

/* ---------------- P12 独立验收 ---------------- */
PAGES.acceptance = async (p) => {
  const [rs, reps] = await Promise.all([
    api("GET", `/projects/${p.id}/runs`), api("GET", `/projects/${p.id}/reports`)]);
  const done = rs.runs.filter(r => r.state === "completed");
  const runOpts = done.map(r => `<option value="${r.id}">${esc(r.id)}（锁定：${esc(r.locked_candidate || "未锁定")}）</option>`).join("");
  const repRows = reps.reports.map(r => `
    <tr><td class="small">${esc(r.id)}</td>
    <td>${pill(r.decision, r.decision === "verified_improvement" ? "ok" :
      r.decision === "regression" ? "bad" : "warn")}</td>
    <td>${(r.stats.diff * 100).toFixed(1)}pp</td>
    <td class="small">n=${r.stats.group_n}（未知${r.stats.unknown}）</td>
    <td><button class="grey" onclick="showReport('${r.id}')">查看</button></td></tr>`).join("");
  return `
  <div class="card"><b>发起独立验收（P12）</b>
    <p class="muted small">必须先锁定候选（TC042）；解封消耗封存测试集，已消耗不能再次作为独立证明（TC043）。
    结论只有五类：verified_improvement / no_improvement / regression / inconclusive / evaluation_invalid。</p>
    <div class="flex"><div><label>已完成的运行</label><select id="acc-run">${runOpts}</select></div>
    <div style="flex:0"><button onclick="acceptRun()">解封并验收</button></div></div>
  </div>
  <div class="card"><b>验收报告</b>
    <table><tr><th>报告</th><th>结论</th><th>配对差异</th><th>规模</th><th></th></tr>${repRows}</table>
    <pre id="rep-out" class="hidden"></pre>
  </div>`;
};
async function acceptRun() {
  const rid = document.getElementById("acc-run").value;
  if (!rid) { toast("暂无可验收的运行", true); return; }
  try { const r = await api("POST", `/runs/${rid}/accept`); toast("验收完成：" + r.decision); route(); }
  catch (e) { toast(e.message, true); }
}
async function showReport(rid) {
  const r = await api("GET", `/reports/${rid}`);
  const s = r.stats;
  const out = document.getElementById("rep-out");
  out.classList.remove("hidden");
  out.textContent = [
    `结论：${r.decision}（不可变报告，TC013）`,
    `主指标：${s.primary_metric || "-"}`,
    `基线可用率 ${(s.baseline_usable_rate * 100).toFixed(1)}% → 候选 ${(s.candidate_usable_rate * 100).toFixed(1)}%，差异 ${(s.diff * 100).toFixed(1)}pp`,
    `修复 ${s.fix} / 退步 ${s.regress} / 双可用 ${s.both} / 双不可用 ${s.neither}；来源n=${s.group_n}`,
    `精确McNemar双侧p=${s.mcnemar_p.toFixed(6)}；bootstrap 95%CI=[${(s.bootstrap.ci_low * 100).toFixed(1)}pp, ${(s.bootstrap.ci_high * 100).toFixed(1)}pp]`,
    `缺失界限：${(s.missing_bounds.low * 100).toFixed(1)}%–${(s.missing_bounds.high * 100).toFixed(1)}%（未知${s.missing_bounds.unknown_n}条不删除）`,
    `严重错误：基线 ${s.severe_baseline}，候选 ${s.severe_candidate}${s.severe_upper_bound_95 != null ? `；零事件95%上界 ${(s.severe_upper_bound_95 * 100).toFixed(2)}%（不等于真实零风险）` : ""}`,
    s.severe_note,
  ].filter(Boolean).join("\n");
}

/* ---------------- P13 使用与反馈 ---------------- */
PAGES.usage = async (p) => {
  const [rel, reps, ps, fb] = await Promise.all([
    api("GET", `/projects/${p.id}/releases`), api("GET", `/projects/${p.id}/reports`),
    api("GET", `/projects/${p.id}/prompts`), api("GET", `/projects/${p.id}/feedback`)]);
  const pvOpts = ps.prompts.map(v => `<option value="${v.id}">${esc(v.name)} v${v.version_no}</option>`).join("");
  const repOpts = `<option value="">（无——只能trial）</option>` + reps.reports
    .map(r => `<option value="${r.id}">${esc(r.id)}（${esc(r.decision)}）</option>`).join("");
  const hist = rel.history.map(h => `
    <tr><td class="small">${esc(h.id)}</td><td class="small">${esc(h.prompt_version_id)}</td>
    <td>${pill(h.status, h.status === "active" ? "ok" : h.status === "trial" ? "warn" : "")}</td>
    <td class="small">${esc(h.created_at)}</td>
    <td>${h.status === "active" ? `<button class="grey" onclick="prepRollback('${h.id}')">准备回滚</button>` : ""}</td></tr>`).join("");
  const fbRows = fb.feedback.map(f => `
    <tr><td class="small">${esc(f.adoption)}</td><td class="small">${esc(f.reason)}</td>
    <td class="small">${esc(f.status)}</td><td class="small">${esc(f.created_at)}</td></tr>`).join("");
  return `
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
      <div><label>验收报告（active 必须绑定 verified 报告 TC048）</label><select id="rel-rep">${repOpts}</select></div>
      <div style="flex:0"><label> </label>
        <button onclick="release('active')">正式采用</button>
        <button class="ghost" onclick="release('trial')">另存试用（不改指针）</button></div>
    </div>
  </div>
  <div class="card"><b>发布历史与回滚</b>
    <table><tr><th>发布</th><th>版本</th><th>状态</th><th>时间</th><th></th></tr>${hist}</table>
    <div class="flex" style="margin-top:8px">
      <div><label>回滚目标发布ID</label><input id="rb-target"></div>
      <div style="flex:0"><label> </label><button class="danger" onclick="rollback()">回滚（产生新事件，不删历史）</button></div>
    </div>
  </div>
  <div class="card"><b>使用反馈</b>
    <div class="flex">
      <div><label>针对发布ID</label><input id="fb-rel" value="${rel.current ? esc(rel.current.id) : ""}"></div>
      <div><label>采用状态</label><select id="fb-adopt"><option value="direct">直接采用</option>
        <option value="minor_edit">轻微修改</option><option value="major_edit">实质修改</option>
        <option value="abandoned">放弃</option></select></div>
      <div><label>修改耗时</label><input id="fb-time" placeholder="如 15分钟"></div>
      <div><label>原因</label><input id="fb-reason"></div>
      <div style="flex:0"><label> </label><button onclick="sendFeedback()">提交反馈</button></div>
    </div>
    <p class="muted small">未反馈按 missing 展示，不视为满意（TC050）；反馈回流进待核验池，不自动成为测试gold。</p>
    <table><tr><th>采用</th><th>原因</th><th>状态</th><th>时间</th></tr>${fbRows}</table>
  </div>`;
};
async function release(mode) {
  try {
    await api("POST", `/projects/${state.pid}/releases`, {
      prompt_version_id: document.getElementById("rel-pv").value,
      report_ref: document.getElementById("rel-rep").value,
      mode });
    toast(mode === "active" ? "已正式采用（指针已更新）" : "已保存为试用，不影响正式指针");
    route();
  } catch (e) { toast(e.message, true); }
}
function prepRollback(id) { document.getElementById("rb-target").value = id; }
async function rollback() {
  const cur = (await api("GET", `/projects/${state.pid}/releases`)).current;
  if (!cur) { toast("当前没有active发布", true); return; }
  try {
    await api("POST", `/releases/${cur.id}/rollback`,
      { target_release_id: document.getElementById("rb-target").value });
    toast("已回滚：恢复目标版本的文本、模型与模板；历史保留");
    route();
  } catch (e) { toast(e.message, true); }
}
async function sendFeedback() {
  try {
    await api("POST", `/releases/${document.getElementById("fb-rel").value}/feedback`, {
      adoption: document.getElementById("fb-adopt").value,
      edit_time: document.getElementById("fb-time").value,
      reason: document.getElementById("fb-reason").value });
    toast("反馈已记录（进入待核验池）");
    route();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- P14 设置 ---------------- */
PAGES.settings = async (p) => {
  const [conns, prices] = await Promise.all([
    api("GET", "/settings/connections"), api("GET", "/settings/prices")]);
  const rows = conns.connections.map(c => `
    <tr><td class="small">${esc(c.id)}</td><td>${esc(c.name)}</td>
    <td>${pill(c.provider, c.provider === "mock" ? "warn" : "brand")}</td>
    <td class="small">${esc(c.model || "-")}</td>
    <td class="small">${c.api_key ? pill("密钥已配置", "ok") : pill("无密钥", "")}</td>
    <td><button class="grey" onclick="testConn('${c.id}')">测试</button></td></tr>`).join("");
  return `
  <div class="card"><b>模型连接</b>
    <p class="muted small">密钥只保存在本机数据库，界面只显示"已配置"，导出与日志不含密钥（TC051）。
    内置模拟供应商可离线跑通全部流程；真实使用请添加 OpenAI 兼容连接（智谱/DeepSeek/OpenAI等）。</p>
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
  <div class="card"><b>价格表（金额硬预算需要全部角色已核实单价 TC032）</b>
    <label>JSON：{"模型名": {"in_per_1k": 0.001, "out_per_1k": 0.002, "currency": "CNY"}}</label>
    <textarea id="price-json">${esc(JSON.stringify(prices.prices, null, 2))}</textarea>
    <div style="margin-top:8px"><button onclick="savePrices()">保存价格表</button></div>
  </div>
  <div class="card"><b>诊断（脱敏导出）</b>
    <button class="ghost" onclick="showDiag()">生成诊断</button>
    <pre id="diag-out" class="hidden"></pre>
  </div>`;
};
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
async function showDiag() {
  const d = await api("GET", "/settings/diagnostics");
  const out = document.getElementById("diag-out");
  out.classList.remove("hidden");
  out.textContent = JSON.stringify(d, null, 2);
}

/* ---------------- 启动 ---------------- */
window.addEventListener("hashchange", route);
route();

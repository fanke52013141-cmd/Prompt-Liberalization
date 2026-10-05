const { chromium } = require('playwright-core');
const fs = require('fs');

const OUT = 'C:/Users/Administrator/Desktop/Prompt-Liberalization/.ux-shots';
const EXEC = 'C:/Users/Administrator/.agent-browser/browsers/chrome-154.0.8037.92/chrome.exe';
const BASE = 'http://127.0.0.1:8765';

(async () => {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  const calls = [];
  await page.route('**/api/**', route => {
    const u = route.request().url();
    const m = u.replace(BASE, '');
    calls.push(m);
    let body = { ok: true };
    if (m.endsWith('/api/runs/1/pairs')) body = { ok: true, pairs: [
      { id: 11, case_id: 101, input: '学员答案：坚持就是胜利。\n教练批注：论据不充分，缺少具体事例支撑。', reference: '',
        a: '【教学点评】该同学的观点有一定道理，但在论据充分性上存在明显不足。建议在论述时补充一至两个具体事例，并交代清楚事例与论点之间的逻辑关系，这样论述才更扎实。',
        b: '【教学点评】观点清晰，但缺少具体事例。建议补充案例。', a_is_candidate: true,
        rating_a: 'minor', rating_b: 'unusable', judged: false, preference: null },
      { id: 12, case_id: 102, input: '学员答案：勿以善小而不为。\n教练批注：立意正确，但表述过于简略。', reference: '',
        a: '【教学点评】立意正确，表述过简。', b: '【教学点评】立意正确，表述过于简略，建议结合事例展开。',
        a_is_candidate: false, rating_a: 'minor', rating_b: 'usable', judged: true, preference: 'b' },
    ] };
    if (m.endsWith('/api/runs/1/verdict')) body = { ok: true, verdict: {
      headline: '新版更好，但样本太少，只能当参考',
      detail_lines: ['2 份对比里，新版更好 1 份、更差 1 份', '原版能直接用的 1 份，新版弄坏了 1 份'],
      sample_warning: '只有 2 份新例子，远低于 15 份的下限。此结论只能当参考，不能作为转正依据。',
      tail: '建议再攒 13 份没考过的新例子，重新考一次。', can_adopt: false,
      regressed_case_ids: [102], improved_case_ids: [] } };
    if (m.endsWith('/api/runs/1')) body = { ok: true,
      run: { id: 1, prompt_id: 1, kind: 'validate', state: 'comparing', case_ids: '[101,102]',
        cap_type: 'requests', cap_value: 40, candidate_version_id: 5 },
      base_done: 2, cand_done: 2, cases_total: 2 };
    if (m.endsWith('/api/runs/1/outputs')) body = { ok: true, outputs: [
      { id: 21, status: 'ok', input_text: '学员答案：坚持就是胜利。\n教练批注：论据不充分。',
        content: '【教学点评】该同学观点有一定道理，但论据不充分。', problem_note: '' },
      { id: 22, status: 'ok', input_text: '学员答案：勿以善小而不为。\n教练批注：表述过简。',
        content: '【教学点评】立意正确。', problem_note: '把对的判成错' },
      { id: 23, status: 'failed', input_text: '学员答案：xxx', error: '模型返回内容为空',
        content: '', problem_note: '' }] };
    if (m.endsWith('/api/prompts')) body = { ok: true, prompts: [
      { id: 1, name: '学员作答点评', cases: 12, usage: 34, official_v: 2, trial_v: 3 },
      { id: 2, name: '公众号标题', cases: 5, usage: 0, official_v: null, trial_v: null }] };
    if (m.endsWith('/api/prompts/1')) body = { ok: true,
      prompt: { id: 1, name: '学员作答点评', official_version_id: 4, trial_version_id: 5 },
      versions: [
        { id: 4, version_no: 2, label: '正式', origin: 'improve', created_at: '2026-10-03 14:20', note: '' },
        { id: 5, version_no: 3, label: '试用', origin: 'improve', created_at: '2026-10-05 09:30', note: '补充了论据要求' },
        { id: 2, version_no: 1, label: '原版', origin: 'import', created_at: '2026-10-01 10:00', note: '' }],
      cases: [
        { id: 101, input_text: '学员答案：坚持就是胜利。', source: 'import', created_at: '2026-10-01', used_in: '[]' },
        { id: 102, input_text: '学员答案：勿以善小而不为。', source: 'usage', created_at: '2026-10-04', used_in: '[]' },
        { id: 103, input_text: '学员答案：知之者不如好之者。', source: 'usage', created_at: '2026-10-04', used_in: '[]' }],
      runs: [ { id: 1, kind: 'validate', state: 'done', created_at: '2026-10-05 09:40', verdict: '{"headline":"新版更好，但样本太少"}' } ] };
    if (m.endsWith('/api/prompts/1/versions/5')) body = { ok: true, version: { version_no: 3, label: '试用', note: '补充了论据要求', content: '你是一名教学点评专家。\n请对学生答案给出点评。' } };
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });

  const shots = [
    ['core-01-home', '#/'],
    ['core-02-prompt', '#/prompt/1'],
    ['core-03-rate', '#/run/1'],
  ];
  for (const [n, h] of shots) {
    await page.goto(BASE + '/' + h, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${OUT}/${n}.png`, fullPage: true });
  }

  // 盲评页：需先进入 comparing 状态
  await page.goto(BASE + '/#/run/1', { waitUntil: 'networkidle' });
  await page.waitForTimeout(700);
  const runState = await page.evaluate(() => document.querySelector('h1') ? document.querySelector('h1').innerText : '');
  await page.screenshot({ path: `${OUT}/core-04-compare.png`, fullPage: true });

  // 提示词详情页
  await page.goto(BASE + '/#/prompt/1', { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/core-05-prompt-detail.png`, fullPage: true });

  // 结论页
  await page.evaluate(() => { window.location.hash = '#/run/1'; });
  await page.waitForTimeout(400);

  // 测量：盲评页两栏是否等高、按钮尺寸
  const metrics = await page.evaluate(() => {
    const panes = [...document.querySelectorAll('.pane')].map(p => Math.round(p.getBoundingClientRect().height));
    const opts = [...document.querySelectorAll('.opt')].map(o => { const r = o.getBoundingClientRect(); return { t: o.textContent.trim(), w: Math.round(r.width), h: Math.round(r.height) }; });
    return { panes, opts };
  });

  // 移动端核心页
  const mob = await ctx.newPage();
  await mob.setViewportSize({ width: 390, height: 844 });
  await mob.route('**/api/**', route => {
    const u = route.request().url(); const m = u.replace(BASE, '');
    let body = { ok: true };
    if (m.endsWith('/api/prompts')) body = { ok: true, prompts: [{ id: 1, name: '学员作答点评', cases: 12, usage: 34, official_v: 2, trial_v: 3 }] };
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await mob.goto(BASE + '/#/new', { waitUntil: 'networkidle' });
  await mob.waitForTimeout(500);
  await mob.screenshot({ path: `${OUT}/core-m-new.png`, fullPage: true });
  const mobNav = await mob.evaluate(() => {
    const n = document.querySelector('.nav');
    const links = [...document.querySelectorAll('.nav a')];
    return {
      navScrollW: n.scrollWidth, navClientW: n.clientWidth,
      visibleLinks: links.filter(a => { const r = a.getBoundingClientRect(); return r.right <= window.innerWidth && r.left >= 0; }).map(a => a.textContent.trim()),
      hiddenLinks: links.filter(a => { const r = a.getBoundingClientRect(); return r.right > window.innerWidth; }).map(a => a.textContent.trim()),
      hasScrollbarHint: getComputedStyle(n).overflowX,
    };
  });

  console.log(JSON.stringify({ runState, metrics, mobNav, errors: [...new Set(errors)] }, null, 2));
  await browser.close();
})();
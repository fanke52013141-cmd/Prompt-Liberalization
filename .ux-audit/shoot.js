const { chromium } = require('playwright-core');
const fs = require('fs');

const OUT = 'C:/Users/Administrator/Desktop/Prompt-Liberalization/.ux-shots';
const EXEC = 'C:/Users/Administrator/.agent-browser/browsers/chrome-154.0.8037.92/chrome.exe';

(async () => {
  if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();

  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  const base = 'http://127.0.0.1:8765';
  const shots = [
    ['lab-01-home', '#/'],
    ['lab-02-new', '#/new'],
    ['lab-03-use', '#/use'],
    ['lab-04-exams', '#/exams'],
    ['lab-05-ledger', '#/ledger'],
    ['lab-06-settings', '#/settings'],
  ];

  const report = [];
  for (const [name, hash] of shots) {
    await page.goto(base + '/' + hash, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    const info = await page.evaluate(() => {
      const m = document.getElementById('app');
      const mRect = m.getBoundingClientRect();
      // 检查横向溢出
      const overflow = document.documentElement.scrollWidth > window.innerWidth + 2;
      // 收集可见交互元素
      const btns = [...document.querySelectorAll('button, .btn, .opt, a.nav, select, input, textarea')];
      const small = btns.filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 32; })
        .map(b => (b.textContent || b.tagName).trim().slice(0, 18) + ' h=' + Math.round(b.getBoundingClientRect().height));
      return {
        mainHeight: Math.round(mRect.height),
        overflowX: overflow,
        scrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        interactiveCount: btns.length,
        tooSmallTargets: [...new Set(small)],
        h1: (document.querySelector('h1') || {}).textContent || '',
      };
    });
    report.push({ page: name, ...info });
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  }

  // 移动端
  const mob = await ctx.newPage();
  await mob.setViewportSize({ width: 390, height: 844 });
  for (const [name, hash] of [['lab-m1-home', '#/'], ['lab-m2-new', '#/new'], ['lab-m3-settings', '#/settings']]) {
    await mob.goto(base + '/' + hash, { waitUntil: 'networkidle' });
    await mob.waitForTimeout(500);
    await mob.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  }
  const mobInfo = await mob.evaluate(() => ({
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
    scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
    navScrollW: document.querySelector('.nav') ? document.querySelector('.nav').scrollWidth : 0,
    navClientW: document.querySelector('.nav') ? document.querySelector('.nav').clientWidth : 0,
  }));

  // 键盘可达性：Tab 遍历统计
  const page2 = await ctx.newPage();
  await page2.goto(base + '/#/settings', { waitUntil: 'networkidle' });
  await page2.waitForTimeout(400);
  const tabReach = [];
  for (let i = 0; i < 30; i++) {
    await page2.keyboard.press('Tab');
    const t = await page2.evaluate(() => {
      const a = document.activeElement;
      if (!a) return null;
      const cs = getComputedStyle(a);
      const r = a.getBoundingClientRect();
      return { tag: a.tagName, txt: (a.textContent || a.value || '').trim().slice(0, 16),
        outline: cs.outlineWidth, outlineStyle: cs.outlineStyle, boxShadow: cs.boxShadow.slice(0, 30),
        vis: r.width > 0 && r.height > 0 };
    });
    if (t) tabReach.push(t);
  }
  const noFocusRing = tabReach.filter(t => t.outline === '0px' && (t.boxShadow === 'none' || t.boxShadow === '')).length;

  // 表单 label 关联检查
  await page2.goto(base + '/#/new', { waitUntil: 'networkidle' });
  await page2.waitForTimeout(400);
  const labelAudit = await page2.evaluate(() => {
    const inputs = [...document.querySelectorAll('input, textarea, select')];
    const out = [];
    document.querySelectorAll('label').forEach(l => {
      const nested = l.querySelectorAll('input, textarea, select').length;
      const forAttr = l.getAttribute('for');
      if (nested === 0 && !forAttr) out.push({ text: l.textContent.trim().slice(0, 20), issue: 'label 无关联控件' });
    });
    return {
      totalControls: inputs.length,
      withForAttr: inputs.filter(i => i.getAttribute('id') && document.querySelector(`label[for="${i.getAttribute('id')}"]`)).length,
      wrappedInLabel: inputs.filter(i => i.closest('label')).length,
      orphanLabels: out,
      placeholderAsLabel: inputs.filter(i => i.closest('label') && !i.closest('label').querySelector('span') && !i.closest('label').textContent.trim()).length,
    };
  });

  console.log(JSON.stringify({ report, mobInfo, noFocusRing, tabCount: tabReach.length, tabSample: tabReach.slice(0, 8), labelAudit, errors: [...new Set(errors)] }, null, 2));
  await browser.close();
})();
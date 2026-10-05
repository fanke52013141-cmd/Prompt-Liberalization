const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const OUT = 'C:/Users/Administrator/Desktop/Prompt-Liberalization/.ux-shots';
const WEB = 'C:/Users/Administrator/Desktop/Prompt-Liberalization/web';
const EXEC = 'C:/Users/Administrator/.agent-browser/browsers/chrome-154.0.8037.92/chrome.exe';

// 用本地静态服务器加载主版本（file:// 会被 fetch 拦截）
const http = require('http');

(async () => {
  const server = http.createServer((req, res) => {
    const p = path.join(WEB, decodeURIComponent(req.url.split('?')[0]));
    const f = fs.existsSync(p) && fs.statSync(p).isFile() ? p : path.join(WEB, 'index.html');
    const ext = path.extname(f);
    const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[ext] || 'text/plain';
    res.writeHead(200, { 'Content-Type': mime + '; charset=utf-8' });
    res.end(fs.readFileSync(f));
  });
  await new Promise(r => server.listen(8899, '127.0.0.1', r));

  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  // 拦截后端 API，返回最小可用桩数据
  await page.route('**/workflow-api/v1/**', route => {
    const url = route.request().url();
    const body = url.includes('/projects') ? { ok: true, projects: [
      { id: 'p1', name: '学员作答点评', status: 'active', task_type: 'review', created_at: '2026-10-01 10:00' },
      { id: 'p2', name: '示例：公众号标题', status: 'active', task_type: 'title', created_at: '2026-09-28 09:00' },
    ] } : { ok: true };
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.goto('http://127.0.0.1:8899/#/projects', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/main-01-projects.png`, fullPage: true });

  const info = await page.evaluate(() => {
    const nav = document.getElementById('nav');
    const links = nav ? [...nav.querySelectorAll('a')].map(a => a.textContent.trim()) : [];
    return {
      sidebarLinks: links.length,
      linkText: links,
      groups: nav ? [...nav.querySelectorAll('.group')].map(g => g.textContent.trim()) : [],
      mainChildren: document.getElementById('main').children.length,
      mainText: document.getElementById('main').innerText.slice(0, 300),
      sidebarWidth: nav ? Math.round(nav.closest('#sidebar').getBoundingClientRect().width) : 0,
    };
  });

  // 全局 CSS token 盘点
  const tokens = await page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    const names = ['--bg','--panel','--line','--ink','--muted','--brand','--ok','--warn','--bad','--radius','--sp-1','--sp-2','--sp-3','--sp-4','--sp-5','--sp-6','--shadow','--faint','--brand-soft','--info-soft'];
    const out = {};
    names.forEach(n => { const v = cs.getPropertyValue(n); out[n] = v ? v.trim() : 'MISSING'; });
    return out;
  });

  // 术语悬停解释机制
  const termInfo = await page.evaluate(() => {
    const t = document.querySelector('.term');
    return { termCount: document.querySelectorAll('.term').length, hasTitle: t ? t.getAttribute('title') : null };
  });

  console.log(JSON.stringify({ info, tokens, termInfo, errors }, null, 2));
  await browser.close();
  server.close();
})();
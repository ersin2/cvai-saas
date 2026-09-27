// Browser checks for CVAI: every page at desktop and phone width, plus the
// flows that only exist in a browser. See e2e/README.md.
//
//   node e2e/run.js            (the app on E2E_BASE, default http://127.0.0.1:8765)
//
// Exits 1 on any failure. Screenshots and a report go to e2e/out/.
'use strict';

const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8765';
const OUT = path.join(__dirname, 'out');
const PASSWORD = 'demo-pass-123';
const CHROME = process.env.CHROME_PATH || (process.platform === 'win32'
  ? 'C:/Program Files/Google/Chrome/Application/chrome.exe'
  : process.platform === 'darwin'
    ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    : '/usr/bin/google-chrome');

// [name, path, user, expected status]
const PAGES = [
  ['landing', '/', null, 200],
  ['login', '/login/', null, 200],
  ['register', '/register/', null, 200],
  ['password-reset', '/password-reset/', null, 200],
  ['terms', '/terms/', null, 200],
  ['privacy', '/privacy/', null, 200],
  ['pricing-anon', '/pricing/', null, 200],
  ['not-found', '/this-does-not-exist/', null, 404],
  ['dashboard', '/dashboard/', 'demo', 200],
  ['studio', '/home/', 'demo', 200],
  ['tools', '/tools/', 'demo', 200],
  ['tracker', '/tracker/', 'demo', 200],
  ['history', '/history/', 'demo', 200],
  ['pricing', '/pricing/', 'demo', 200],
  ['profile', '/profile/', 'demo', 200],
  ['pricing-free', '/pricing/', 'freebie', 200],
  ['staff-ai-usage', '/staff/ai-usage/', 'ops', 200],
];
const VIEWPORTS = [['desktop', 1440, 900], ['phone', 390, 844]];

const failures = [];
const fail = (where, what) => { failures.push(`${where}: ${what}`); console.log(`  FAIL ${where}: ${what}`); };
const wait = ms => new Promise(r => setTimeout(r, ms));

// Text contrast, run in the page: each visible text node against its
// effective background (WCAG: 4.5:1, or 3:1 for large text).
function contrastAudit() {
  function parse(c) {
    const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum({ r, g, b }) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }
  function blend(top, bottom) {
    const a = top.a;
    return { r: top.r * a + bottom.r * (1 - a), g: top.g * a + bottom.g * (1 - a), b: top.b * a + bottom.b * (1 - a), a: 1 };
  }
  function background(el) {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage && cs.backgroundImage !== 'none' && !cs.backgroundImage.startsWith('url')) return null;
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a >= 1) break; }
    }
    let out = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = stack.length - 1; i >= 0; i--) out = blend(stack[i], out);
    return out;
  }
  const bad = [];
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode;
    if (!t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    if (el.closest('[aria-hidden="true"], script, style, noscript, .machine-panel, .lp-machine, iframe, [data-contrast-skip]')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    let hidden = false;
    for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) { hidden = true; break; }
    }
    if (hidden) continue;
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const bg = background(el);
    if (!fg || !bg) continue;
    const L1 = lum(blend(fg, bg)), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const min = (size >= 24 || (size >= 18.66 && weight >= 700)) ? 3 : 4.5;
    if (ratio < min) bad.push(`${ratio.toFixed(2)}:1 < ${min} "${t.textContent.trim().slice(0, 40)}" (${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]})`);
  }
  return bad;
}

// Icons are CSS masks (static/generator/css/icons.css); one without a mask
// image draws nothing.
function blankIcons() {
  return [...document.querySelectorAll('i[class*="fa-"]')]
    .filter(e => { const cs = getComputedStyle(e); const m = cs.webkitMaskImage || cs.maskImage; return !m || m === 'none'; })
    .map(e => e.className);
}

async function main() {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: process.env.CI ? ['--no-sandbox'] : [],
  });

  const cookies = {};
  async function sessionFor(user) {
    if (!cookies[user]) {
      const ctx = await browser.createBrowserContext();
      const p = await ctx.newPage();
      await p.goto(BASE + '/login/');
      await p.type('#id_username', user);
      await p.type('#id_password', PASSWORD);
      await Promise.all([p.waitForNavigation(), p.click('button[type=submit]')]);
      if (p.url().includes('/login/')) throw new Error(`could not sign in as ${user} — was e2e/seed.py run?`);
      cookies[user] = await p.cookies();
      await ctx.close();
    }
    return cookies[user];
  }

  // A fresh context per page, so sessions never mix.
  async function open(user, width = 1440, height = 900) {
    const ctx = await browser.createBrowserContext();
    const page = await ctx.newPage();
    await page.setViewport({ width, height });
    if (user) await page.setCookie(...(await sessionFor(user)));
    page.errors = [];
    page.on('pageerror', e => page.errors.push(e.message));
    page.on('response', r => {
      const url = r.url();
      if (r.status() >= 400 && url.startsWith(BASE) && r.request().resourceType() !== 'document'
          && !url.endsWith('/favicon.ico')) {
        page.errors.push(`${r.status()} ${url.slice(BASE.length)}`);
      }
    });
    page.close = () => ctx.close();
    return page;
  }

  async function shot(page, name) {
    await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true }).catch(() => {});
  }

  // ── Every page, both widths ──────────────────────────────────────────────
  console.log('Pages');
  for (const [name, url, user, status] of PAGES) {
    for (const [label, width, height] of VIEWPORTS) {
      const where = `${name} @${label}`;
      const page = await open(user, width, height);
      const resp = await page.goto(BASE + url, { waitUntil: 'networkidle0', timeout: 60000 });
      if (!resp || resp.status() !== status) fail(where, `HTTP ${resp && resp.status()}, expected ${status}`);
      await page.evaluate(() => document.querySelectorAll('[class*="anim-"]').forEach(e => {
        e.style.animation = 'none'; e.style.opacity = 1; e.style.transform = 'none';
      }));
      await wait(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (overflow > 1) fail(where, `page scrolls sideways by ${overflow}px`);
      const icons = await page.evaluate(blankIcons);
      if (icons.length) fail(where, `icons without a drawing: ${icons.join(', ')}`);
      if (label === 'desktop') {
        const low = await page.evaluate(contrastAudit);
        low.forEach(line => fail(where, `low contrast ${line}`));
      }
      page.errors.forEach(e => fail(where, e));
      await shot(page, `${name}-${label}`);
      await page.close();
    }
    console.log(`  ok ${name}`);
  }

  // ── Toasts and the confirm dialog ────────────────────────────────────────
  console.log('Flows');
  {
    const page = await open('demo');
    await page.goto(BASE + '/dashboard/', { waitUntil: 'networkidle0' });
    await page.evaluate(() => ['success', 'warning', 'danger', 'info'].forEach(t => showToast(`A ${t} message`, t)));
    await wait(500);
    const n = await page.$$eval('.toast-item.show', els => els.length);
    if (n !== 4) fail('toasts', `${n} of 4 toasts visible`);
    const icons = await page.evaluate(blankIcons);
    if (icons.length) fail('toasts', `icons without a drawing: ${icons.join(', ')}`);
    const low = await page.evaluate(contrastAudit);
    low.filter(l => l.includes('message')).forEach(l => fail('toasts', `low contrast ${l}`));
    await shot(page, 'flow-toasts');
    page.errors.forEach(e => fail('toasts', e));
    await page.close();
    console.log('  ok toasts');
  }

  // ── AI Tools: an answer streams in, the page keeps what was typed ───────
  {
    const where = 'tools stream';
    const page = await open('demo');
    await page.goto(BASE + '/tools/', { waitUntil: 'networkidle0' });
    await page.click(`button[onclick*="'interview'"]`);
    await page.type('#form-interview input[name=company_name]', 'Kept Company');
    await page.click(`button[onclick*="'ats'"]`);
    await page.$eval('#form-ats textarea[name=resume]', el => { el.value = 'Python Django engineer'; });
    await page.type('#form-ats textarea[name=job_description]', 'Backend engineer, Kubernetes');
    const before = await page.$$eval('#toolResults .tool-result-card', els => els.length);
    await page.click('#form-ats button[type=submit]');

    const lengths = new Set();
    const t0 = Date.now();
    let finished = false;
    while (Date.now() - t0 < 30000) {
      const s = await page.$eval('#toolResultContent', el => ({ len: el.textContent.length, live: el.classList.contains('is-streaming') }));
      if (s.live) lengths.add(s.len);
      const count = await page.$$eval('#toolResults .tool-result-card', els => els.length);
      if (!s.live && count > before) { finished = true; break; }
      await wait(60);
    }
    if (!finished) fail(where, 'the answer never finished');
    if (lengths.size < 3) fail(where, `text did not arrive progressively (${lengths.size} partial renders)`);
    const content = await page.$eval('#toolResultContent', el => el.textContent);
    if (!content.includes('74')) fail(where, 'the finished answer does not show the ATS score');
    const kept = await page.$eval('#form-interview input[name=company_name]', el => el.value);
    if (kept !== 'Kept Company') fail(where, `another tool lost its input (now "${kept}")`);
    await shot(page, 'flow-tools-stream');
    page.errors.forEach(e => fail(where, e));
    await page.close();
    console.log(`  ok tools stream (${lengths.size} partial renders)`);
  }

  // ── Cover letter: tabs appear as sections stream in, no raw tags ─────────
  {
    const where = 'cover letter stream';
    const page = await open('demo');
    await page.goto(BASE + '/tools/', { waitUntil: 'networkidle0' });
    await page.type('#form-cover input[name=company_name]', 'Northwind');
    await page.type('#form-cover input[name=job_title]', 'Backend Engineer');
    await page.$eval('#form-cover textarea[name=resume]', el => { el.value = 'Maria Keller, backend engineer'; });
    await page.type('#form-cover textarea[name=job_description]', 'Payments backend role');
    await page.click('#form-cover button[type=submit]');
    const tabCounts = new Set();
    const t0 = Date.now();
    let s = {};
    while (Date.now() - t0 < 30000) {
      s = await page.evaluate(() => ({
        tabs: document.querySelectorAll('#coverSectionTabs .tool-tab').length,
        live: document.querySelector('#coverSectionContent').classList.contains('is-streaming'),
        text: document.querySelector('#coverSectionContent').textContent,
      }));
      if (s.live) tabCounts.add(s.tabs);
      if (/\[(SECTION|END_SECTION)/.test(s.text)) { fail(where, 'a raw section tag is visible'); break; }
      if (!s.live && s.tabs === 5) break;
      await wait(60);
    }
    if (s.tabs !== 5) fail(where, `${s.tabs} of 5 section tabs`);
    if (tabCounts.size < 2) fail(where, 'the tabs did not appear section by section');
    if (!s.text.includes('Dear Northwind')) fail(where, 'the letter is not shown');
    await shot(page, 'flow-cover-stream');
    page.errors.forEach(e => fail(where, e));
    await page.close();
    console.log('  ok cover letter stream');
  }

  // ── A throttled request explains itself ──────────────────────────────────
  {
    const where = 'throttle toast';
    const page = await open('demo');
    await page.setRequestInterception(true);
    page.on('request', req => {
      if (req.method() === 'POST' && req.url().endsWith('/ats-score/')) {
        req.respond({ status: 429, contentType: 'application/json',
                      body: JSON.stringify({ error: 'Too many requests in a row. Wait about a minute.' }) });
      } else {
        req.continue();
      }
    });
    await page.goto(BASE + '/tools/', { waitUntil: 'networkidle0' });
    await page.click(`button[onclick*="'ats'"]`);
    await page.type('#form-ats textarea[name=job_description]', 'Anything');
    await page.click('#form-ats button[type=submit]');
    await page.waitForSelector('.toast-item.toast-warning', { timeout: 5000 }).catch(() => {});
    const toast = await page.$eval('.toast-item.toast-warning', el => el.textContent).catch(() => '');
    if (!toast.includes('Wait about a minute')) fail(where, 'no warning toast with the server message');
    const disabled = await page.$eval('#form-ats button[type=submit]', b => b.disabled);
    if (disabled) fail(where, 'the submit button stayed disabled');
    page.errors.filter(e => !e.startsWith('429')).forEach(e => fail(where, e));
    await page.close();
    console.log('  ok throttle toast');
  }

  // ── Staff page is staff-only ─────────────────────────────────────────────
  {
    const page = await open('demo');
    await page.goto(BASE + '/staff/ai-usage/', { waitUntil: 'networkidle0' });
    if (!page.url().includes('/login/')) fail('staff page', `a member reached it (${page.url()})`);
    await page.close();
    console.log('  ok staff page is staff-only');
  }

  // ── Delete account: cancel keeps it, confirm removes it ──────────────────
  {
    const where = 'delete account';
    const page = await open('leaver');
    await page.goto(BASE + '/profile/', { waitUntil: 'networkidle0' });
    await page.click('#deleteAccountBtn');
    await page.waitForSelector('#confirmOverlay.open', { timeout: 3000 }).catch(() => fail(where, 'no confirm dialog'));
    await shot(page, 'flow-delete-confirm');
    await page.click('#confirmCancel');
    await wait(300);
    if (!page.url().includes('/profile/')) fail(where, 'cancel did not keep the user on the page');
    await page.click('#deleteAccountBtn');
    await page.waitForSelector('#confirmOverlay.open', { timeout: 3000 });
    await Promise.all([page.waitForNavigation({ timeout: 10000 }).catch(() => {}), page.click('#confirmOk')]);
    if (new URL(page.url()).pathname !== '/') fail(where, `not sent to the landing page (${page.url()})`);
    page.errors.forEach(e => fail(where, e));
    await page.close();

    const again = await open(null);
    await again.goto(BASE + '/login/');
    await again.type('#id_username', 'leaver');
    await again.type('#id_password', PASSWORD);
    await Promise.all([again.waitForNavigation(), again.click('button[type=submit]')]);
    if (!again.url().includes('/login/')) fail(where, 'the deleted account can still sign in');
    await again.close();
    console.log('  ok delete account');
  }

  await browser.close();
  fs.writeFileSync(path.join(OUT, 'report.txt'), failures.join('\n') || 'all checks passed');
  if (failures.length) {
    console.log(`\n${failures.length} failure(s). Screenshots in ${OUT}`);
    process.exit(1);
  }
  console.log('\nAll browser checks passed.');
}

main().catch(err => { console.error(err); process.exit(1); });

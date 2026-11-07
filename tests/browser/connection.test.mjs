import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const run = promisify(execFile);
const scriptVersion = /^\/\/\s*@version\s+(\S+)/m.exec(await readFile(new URL('../../userscript/metadata.txt', import.meta.url), 'utf8'))[1];
let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser.close(); });

// Independent generated-userscript harness: every GM request terminates at a
// temporary local HTTP fixture. No OS protocol handler or real model is invoked.
async function fixture({ offline = false, healthApp = 'plamo-for-cp', automatic = false, healthState = 'idle', available = true, healthDelay = 0, clock = false, holdPoll = false, availableVersion } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'cp-translator-connection-'));
  const context = await browser.newContext();
  const control = {
    offline, healthApp, healthStatus: 200, healthDelay, setupDelay: 0, holdPoll, availableVersion,
    requests: [], jobs: new Map(), opened: [], values: {
      'cp-translator-model': '2b', 'cp-translator-glossary': [],
      'cp-translator-auto-sites': { codeforces: automatic },
    },
    health: { loaded_model: null, state: healthState },
    setup: {
      application: 'plamo-for-cp', selected_model: '2b', default_glossary: [],
      supported_translation_jobs: true, supported_translation_previews: true,
      supported_batch_sizes: [1, 2, 4, 8, 16],
      device: { system: 'Darwin', memory_gb: 24, supported: true },
      models: ['0.8b', '2b', '8b'].map(id => ({ id, weight_gb: 2.41, compatible: true, available })),
      preparation: { stage: 'idle', model: null },
    },
  };
  const timers = new Set();
  const later = (delay, callback) => { const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay); timers.add(timer); };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    let raw = ''; for await (const piece of request) raw += piece;
    const body = raw ? JSON.parse(raw) : null;
    const entry = { path: url.pathname, method: request.method, headers: request.headers, body, aborted: false };
    control.requests.push(entry);
    response.on('close', () => { if (!response.writableEnded) entry.aborted = true; });
    if (control.offline) { response.destroy(); return; }
    const send = (value, status = 200) => { if (!response.destroyed) { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); } };
    if (url.pathname === '/api/health') { later(control.healthDelay, () => send({ status: 'ok', app: control.healthApp, engine: control.health, userscript_version: control.availableVersion }, control.healthStatus)); return; }
    if (url.pathname === '/api/setup') { later(control.setupDelay, () => send(control.setup)); return; }
    if (url.pathname === '/api/translation-jobs' && request.method === 'POST') {
      const id = '00000000-0000-4000-8000-' + String(control.jobs.size + 1).padStart(12, '0');
      control.jobs.set(id, body.segments.map((segment, index) => ({ seq: index + 1, type: 'translation', id: segment.id, text: '日本語: ' + segment.text, cached: false })));
      send({ id, state: 'queued', cursor: 0, total: body.segments.length }, 202); return;
    }
    const match = /^\/api\/translation-jobs\/([a-f0-9-]+)(\/cancel)?$/.exec(url.pathname);
    if (match) {
      const events = control.jobs.get(match[1]) || [];
      if (match[2]) send({ id: match[1], state: 'cancelled' });
      else if (!control.holdPoll) send({ id: match[1], state: 'completed', events: events.filter(event => event.seq > Number(url.searchParams.get('after'))), previews: [], cursor: events.length, completed: events.length, total: events.length, cached: 0 });
      return;
    }
    send({ detail: 'Unexpected fixture route' }, 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const output = join(directory, 'generated.user.js');
  await run(process.execPath, ['scripts/build_userscript.mjs', '--api-port', String(port), '--output', output]);
  const source = await readFile(output, 'utf8');
  const pending = new Map();
  await context.exposeBinding('__connectionRequest', async ({ page }, id, details) => {
    const controller = new AbortController();
    if (!pending.has(page)) pending.set(page, new Map());
    pending.get(page).set(id, controller);
    try {
      const response = await fetch(details.url, { method: details.method, headers: details.headers, body: details.data, redirect: 'error', signal: controller.signal });
      return { status: response.status, responseText: await response.text(), finalUrl: response.url };
    } finally { pending.get(page).delete(id); }
  });
  await context.exposeBinding('__connectionAbort', ({ page }, id) => pending.get(page)?.get(id)?.abort());
  await context.exposeBinding('__connectionOpen', (_source, url) => control.opened.push(url));
  const page = await context.newPage();
  if (clock) {
    // Pause before navigation, while no product timers exist. A fixed future
    // target avoids an evaluate→pauseAt round-trip racing against CPU load.
    await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
    await page.clock.pauseAt(new Date('2026-01-02T00:00:00Z'));
  }
  control.pageErrors = [];
  page.on('pageerror', error => control.pageErrors.push(error.message));
  page.__connectionControl = control;
  page.setDefaultTimeout(6000);
  await page.addInitScript(({ source, values }) => {
    const listeners = []; let nextRequest = 1;
    window.__gmValues = values;
    window.__gmSetupCount = 0;
    const update = (key, value) => { const old = values[key]; values[key] = value; if (key === 'cp-translator-translation-previews') window.__gmSetupCount++; for (const listener of listeners) if (listener.key === key) listener.callback(key, old, value, false); };
    window.GM_getValue = (key, fallback) => Object.hasOwn(values, key) ? values[key] : fallback;
    window.GM_setValue = update;
    window.GM_deleteValue = key => { delete values[key]; };
    window.GM_addValueChangeListener = (key, callback) => listeners.push({ key, callback });
    window.GM_registerMenuCommand = () => {};
    window.GM_openInTab = url => window.__connectionOpen(url);
    window.GM_xmlhttpRequest = details => {
      const id = nextRequest++; let done = false;
      window.__connectionRequest(id, { url: details.url, method: details.method || 'GET', headers: details.headers, data: details.data }).then(value => { if (!done) { done = true; details.onload?.(value); } }).catch(() => { if (!done) { done = true; details.onerror?.(); } });
      return { abort() { if (!done) { done = true; window.__connectionAbort(id); details.onabort?.(); } } };
    };
    (0, eval)(source);
  }, { source, values: control.values });
  await page.route('https://codeforces.com/contest/1/problem/A', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<!doctype html><meta charset="utf-8"><div class="problem-statement"><p id="statement">Find the minimum answer.</p></div>' }));
  await page.goto('https://codeforces.com/contest/1/problem/A');
  if (clock) await page.clock.runFor(1);
  return {
    page, control, api: `http://127.0.0.1:${port}`,
    async close() { await context.close(); for (const timer of timers) clearTimeout(timer); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(directory, { recursive: true, force: true }); },
  };
}

test('an older installed userscript gets a fixed update link without blocking a running translation', async () => {
  const harness = await fixture({ availableVersion: '999.0.0', holdPoll: true });
  const { page, control, api } = harness;
  try {
    await state(page, 'connected');
    const update = page.getByRole('link', { name: '翻訳スクリプトを更新', exact: true });
    assert.equal(await update.isVisible(), true);
    assert.equal(await update.getAttribute('href'), api + '/install.user.js');
    assert.match(await update.getAttribute('title'), new RegExp(`現在 v${scriptVersion.replaceAll('.', '\\.')} → v999\\.0\\.0`));
    assert.match(await update.getAttribute('title'), /再読み込み/);
    assert.equal(await page.evaluate(() => typeof GM_info), 'undefined');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await eventually(() => control.jobs.size === 1);
    assert.equal(await page.getByRole('button', { name: '停止', exact: true }).isVisible(), true);
    assert.equal(await page.getByRole('button', { name: '原文を表示', exact: true }).isEnabled(), true);
    assert.equal(await update.isVisible(), true);
    await page.context().route(api + '/install.user.js', route => route.fulfill({ contentType: 'text/plain', body: 'userscript installer' }));
    const opened = page.waitForEvent('popup');
    await update.click();
    const installer = await opened;
    await installer.waitForLoadState();
    assert.equal(installer.url(), api + '/install.user.js');
    await installer.close();
    assert.equal(await page.getByRole('button', { name: '停止', exact: true }).isVisible(), true);
    await page.getByRole('button', { name: '停止', exact: true }).click();
  } finally { await harness.close(); }
});

test('same, older, missing or malformed app versions leave translation usable without an update prompt', async () => {
  for (const availableVersion of [scriptVersion, '0.0.1', undefined, null, 'https://example.org/installer', '<img src=x onerror=alert(1)>', '999.0.0-beta']) {
    const harness = await fixture({ availableVersion });
    try {
      await state(harness.page, 'connected');
      assert.equal(await harness.page.getByRole('link', { name: '翻訳スクリプトを更新', exact: true }).isVisible(), false);
      await harness.page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
      await harness.page.getByRole('button', { name: '原文を表示', exact: true }).waitFor();
      assert.equal(harness.control.jobs.size, 1);
      assert.deepEqual(harness.control.pageErrors, []);
    } finally { await harness.close(); }
  }
});

test('periodic health checks notice an app upgrade without changing translation preferences', async () => {
  const harness = await fixture({ availableVersion: scriptVersion, clock: true });
  try {
    await state(harness.page, 'connected');
    const before = await harness.page.evaluate(() => JSON.stringify(window.__gmValues));
    const update = harness.page.getByRole('link', { name: '翻訳スクリプトを更新', exact: true });
    assert.equal(await update.isVisible(), false);
    harness.control.availableVersion = '999.0.0';
    await harness.page.clock.runFor(30001);
    await update.waitFor();
    assert.equal(await harness.page.evaluate(() => JSON.stringify(window.__gmValues)), before);
    harness.control.availableVersion = scriptVersion;
    await harness.page.clock.runFor(30001);
    await update.waitFor({ state: 'hidden' });
  } finally { await harness.close(); }
});

async function state(page, value) {
  try { await page.locator(`#cp-translator .panel[data-connection="${value}"]`).waitFor(); }
  catch (error) { error.message += '\nConnection fixture: ' + JSON.stringify({ requests: page.__connectionControl.requests, pageErrors: page.__connectionControl.pageErrors, ui: await page.locator('#cp-translator').evaluate(host => host.shadowRoot?.innerHTML).catch(() => null) }); throw error; }
}

async function eventually(predicate) {
  for (let attempt = 0; attempt < 300; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(predicate(), 'Expected local request transition did not happen');
}

async function captureToolbar(page, path) {
  const bounds = await page.locator('#cp-translator').boundingBox();
  const viewport = page.viewportSize();
  const x = Math.max(0, bounds.x - 12), y = Math.max(0, bounds.y - 50);
  await page.screenshot({ path, clip: { x, y, width: Math.min(viewport.width - x, bounds.width + 24), height: Math.min(viewport.height - y, bounds.height + 64) } });
}

test('offline toolbar exposes only fixed launch link and trusted reconnect recovers capabilities before automatic translation', async () => {
  const harness = await fixture({ offline: true, automatic: true });
  try {
    const { page, control } = harness;
    await state(page, 'disconnected');
    await captureToolbar(page, '.local/offline-toolbar.png');
    assert.equal(await page.getByRole('link', { name: 'アプリを起動' }).getAttribute('href'), 'plamo-for-cp://open');
    assert.equal(control.requests.filter(request => request.method === 'POST').length, 0);
    const prevented = await page.evaluate(() => {
      const shadow = document.querySelector('#cp-translator').shadowRoot;
      const link = shadow.querySelector('[aria-label="アプリを起動"]');
      // Temporarily use a harmless same-page target so even a broken synthetic
      // event guard cannot invoke a real OS handler during this regression.
      const href = link.getAttribute('href');
      link.setAttribute('href', '#audit-no-launch');
      Object.defineProperty(link, 'href', { configurable: true, get: () => '#audit-no-launch', set() {} });
      const event = new MouseEvent('click', { bubbles: true, cancelable: true });
      try { link.dispatchEvent(event); return event.defaultPrevented; }
      finally { delete link.href; link.setAttribute('href', href); }
    });
    assert.equal(prevented, true);
    assert.deepEqual(control.opened, []);
    control.offline = false;
    control.healthDelay = 500;
    await page.getByRole('button', { name: '再接続', exact: true }).click();
    await state(page, 'checking');
    await captureToolbar(page, '.local/checking-toolbar.png');
    await state(page, 'connected');
    await page.waitForFunction(() => document.querySelector('#statement').textContent.startsWith('日本語: '));
    await captureToolbar(page, '.local/reconnected-toolbar.png');
    const setupIndex = control.requests.findIndex(request => request.path === '/api/setup');
    const translateIndex = control.requests.findIndex(request => request.path === '/api/translation-jobs' && request.method === 'POST');
    assert.ok(setupIndex >= 0 && translateIndex > setupIndex);
    assert.equal(await page.evaluate(() => window.__gmValues['cp-translator-translation-previews']), true);
    assert.ok(control.requests.every(request => request.headers['x-plamo-client'] === 'userscript'));
    assert.ok(control.requests.every(request => !request.path.includes('download') && !request.path.includes('prepare')));
  } finally { await harness.close(); }
});

test('a foreign app on the loopback port never becomes connected or receives a setup request', async () => {
  const harness = await fixture({ healthApp: 'unrelated-app' });
  try {
    await state(harness.page, 'disconnected');
    assert.equal(harness.control.requests.filter(request => request.path === '/api/setup').length, 0);
    assert.equal(harness.control.requests.filter(request => request.method === 'POST').length, 0);
  } finally { await harness.close(); }
});

test('missing loaded weights and another active translation remain connected without implicit downloads', async () => {
  const harness = await fixture({ healthState: 'translating', available: false });
  try {
    await state(harness.page, 'connected');
    assert.equal(harness.control.requests.filter(request => request.method === 'POST').length, 0);
    assert.equal(await harness.page.getByRole('link', { name: 'アプリを起動' }).isVisible(), false);
  } finally { await harness.close(); }
});

test('a request-side disconnect restores offline UI and recovery refreshes setup without consuming the page', async () => {
  const harness = await fixture();
  try {
    const { page, control } = harness;
    await state(page, 'connected');
    const original = await page.locator('#statement').textContent();
    control.offline = true;
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await state(page, 'disconnected');
    assert.equal(await page.locator('#statement').textContent(), original);
    const attempts = control.requests.filter(request => request.method === 'POST');
    assert.ok(attempts.length <= 1);
    assert.ok(attempts.every(request => request.path === '/api/translation-jobs'));
    control.offline = false;
    await page.getByRole('button', { name: '再接続', exact: true }).click();
    await state(page, 'connected');
    assert.equal(control.requests.filter(request => request.path === '/api/setup').length, 2);
    assert.equal(control.requests.filter(request => request.method === 'POST').length, attempts.length);
  } finally { await harness.close(); }
});

test('closing a checking toolbar cancels its only in-flight health request and ignores the late response', async () => {
  const harness = await fixture({ healthDelay: 900 });
  try {
    const { page, control } = harness;
    await state(page, 'checking');
    await eventually(() => control.requests.some(request => request.path === '/api/health'));
    await page.getByRole('button', { name: 'パネルを閉じる', exact: true }).click();
    await eventually(() => control.requests.find(request => request.path === '/api/health')?.aborted);
    await page.waitForTimeout(1000);
    assert.equal(await page.locator('#cp-translator').isVisible(), false);
    assert.equal(control.requests.filter(request => request.path === '/api/health').length, 1);
    assert.equal(control.requests.filter(request => request.path === '/api/setup').length, 0);
  } finally { await harness.close(); }
});

test('visibility and page-cache lifecycle stop probes and resume one healthy check without reloading setup', async () => {
  const harness = await fixture({ clock: true });
  try {
    const { page, control } = harness;
    await state(page, 'connected');
    const before = control.requests.filter(request => request.path === '/api/health').length;
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.clock.fastForward(120000);
    assert.equal(control.requests.filter(request => request.path === '/api/health').length, before);
    await page.evaluate(() => {
      delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.clock.runFor(5);
    await eventually(() => control.requests.filter(request => request.path === '/api/health').length === before + 1);
    await state(page, 'connected');
    const resumed = control.requests.filter(request => request.path === '/api/health').length;
    await page.evaluate(() => dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })));
    await page.clock.fastForward(120000);
    assert.equal(control.requests.filter(request => request.path === '/api/health').length, resumed);
    await page.evaluate(() => dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
    await page.clock.runFor(5);
    await eventually(() => control.requests.filter(request => request.path === '/api/health').length === resumed + 1);
    await state(page, 'connected');
    // Resuming uses a fresh setup snapshot; an ordinary connected heartbeat
    // below only probes health and cannot repeatedly rewrite preferences.
    const setups = control.requests.filter(request => request.path === '/api/setup').length;
    await page.clock.fastForward(30000);
    await eventually(() => control.requests.filter(request => request.path === '/api/health').length === resumed + 2);
    await state(page, 'connected');
    assert.equal(control.requests.filter(request => request.path === '/api/setup').length, setups);
  } finally { await harness.close(); }
});

test('offline polling backs off rather than repeatedly probing a missing local app', async () => {
  const harness = await fixture({ offline: true, clock: true });
  try {
    const { page, control } = harness;
    await state(page, 'disconnected');
    const probes = () => control.requests.filter(request => request.path === '/api/health').length;
    const before = probes();
    await page.clock.fastForward(1000);
    assert.equal(probes(), before);
    await page.clock.fastForward(3000);
    await eventually(() => probes() === before + 1);
    await state(page, 'disconnected');
    await page.clock.fastForward(1000);
    assert.equal(probes(), before + 1);
    await page.clock.fastForward(6000);
    await eventually(() => probes() === before + 2);
    await state(page, 'disconnected');
    assert.equal(control.requests.filter(request => request.path === '/api/setup').length, 0);
    assert.equal(control.requests.filter(request => request.method === 'POST').length, 0);
  } finally { await harness.close(); }
});

test('an externally completed model download refreshes availability before automatic translation', async () => {
  const harness = await fixture({ available: false, automatic: true, clock: true });
  try {
    const { page, control } = harness;
    await state(page, 'connected');
    const setupsProcessed = await page.evaluate(() => window.__gmSetupCount);
    assert.equal(control.requests.filter(request => request.method === 'POST').length, 0);
    control.setup.models.find(model => model.id === '2b').available = true;
    await page.clock.fastForward(30000);
    await eventually(() => control.requests.filter(request => request.path === '/api/setup').length === 2);
    await page.waitForFunction(before => window.__gmSetupCount > before, setupsProcessed);
    await state(page, 'connected');
    await page.clock.runFor(150);
    await eventually(() => control.requests.some(request => request.method === 'POST' && request.path === '/api/translation-jobs'));
    assert.ok(control.requests.every(request => !request.path.includes('download') && !request.path.includes('prepare')));
  } finally { await harness.close(); }
});

test('rapid genuine translation clicks share a delayed health probe and start one pipeline', async () => {
  const harness = await fixture();
  try {
    const { page, control } = harness;
    await state(page, 'connected');
    await page.waitForTimeout(1600); // Leave the documented 1.5s probe spacing window.
    control.healthDelay = 300;
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).dblclick({ delay: 40 });
    await page.waitForFunction(() => document.querySelector('#statement').textContent.startsWith('日本語: '));
    assert.equal(control.requests.filter(request => request.path === '/api/translation-jobs' && request.method === 'POST').length, 1);
    assert.equal(control.requests.filter(request => request.path === '/api/health').length, 2);
    assert.equal(control.requests.filter(request => request.path === '/api/setup').length, 1);
  } finally { await harness.close(); }
});

test('a monitor-side disconnect while results are held cancels work and retries the unchanged automatic page after recovery', async () => {
  const harness = await fixture({ automatic: true, holdPoll: true });
  try {
    const { page, control } = harness;
    const creates = () => control.requests.filter(request => request.path === '/api/translation-jobs' && request.method === 'POST');
    await state(page, 'connected');
    await eventually(() => control.requests.some(request => request.method === 'GET' && request.path.startsWith('/api/translation-jobs/')));
    assert.equal(creates().length, 1);
    control.healthStatus = 503;
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
      delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await state(page, 'disconnected');
    await eventually(() => control.requests.some(request => request.path.endsWith('/cancel')));
    assert.equal(await page.locator('#statement').textContent(), 'Find the minimum answer.');
    control.holdPoll = false;
    control.healthStatus = 200;
    await page.getByRole('button', { name: '再接続', exact: true }).click();
    await state(page, 'connected');
    await page.waitForFunction(() => document.querySelector('#statement').textContent.startsWith('日本語: '));
    assert.equal(creates().length, 2);
    assert.equal(control.jobs.size, 2);
  } finally { await harness.close(); }
});

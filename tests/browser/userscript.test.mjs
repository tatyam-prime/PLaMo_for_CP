import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';

const run = promisify(execFile);
let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser?.close(); });

const problem = '<main><div class="problem-statement"><p id="p">You are given positive integers. Find the answer.</p><p>Print the answer.</p></div></main>';
const article = '<article><h1>An article</h1><p id="p">You are given positive integers. Find the answer.</p></article>';
const dashboard = '<!doctype html><meta charset="utf-8"><title>App management</title><main><section id="browser-settings"><p id="browser-settings-placeholder">Install the script.</p></section></main>';
const defaultGlossary = [
  { source: 'modular inverse', target: '逆元' }, { source: 'proper prefix', target: '真の接頭辞' },
  { source: 'parallel edge', target: '多重辺' }, { source: 'prefix sum', target: '累積和' }, { source: 'jury', target: 'ジャッジ' },
  { source: 'subarray', target: '連続部分配列' },
  { source: 'increment', target: 'インクリメント' },
  { source: 'pairwise disjoint', target: '互いに交差しない' },
  { source: 'sequence', target: '列' },
  { source: 'visualizer', target: 'ビジュアライザ' },
  { source: 'forest', target: '森' },
  { source: 'node', target: '頂点' },
];

async function chooseModel(page, model) {
  const select = page.getByLabel('モデルサイズ');
  await select.focus(); await select.press(model[0]); await select.press('Tab');
  assert.equal(await select.inputValue(), model);
}

async function waitForMock(check) {
  const deadline = Date.now() + 5000;
  while (!check()) { assert.ok(Date.now() < deadline, 'Timed out waiting for the local mock API'); await new Promise(resolve => setTimeout(resolve, 10)); }
}

async function fixture(initialValues = {}) {
  const context = await browser.newContext();
  const directory = await mkdtemp(join(tmpdir(), 'cp-translator-userscript-'));
  const control = { values: { ...initialValues }, requests: [], opened: [], delay: 0, status: 200, jobs: new Map(), jobDelays: {}, jobErrors: {}, jobPreviews: {}, creationDelay: 0, setup: { application: 'plamo-for-cp', selected_model: '2b', default_glossary: structuredClone(defaultGlossary), supported_batch_sizes: Array.from({ length: 32 }, (_, index) => index + 1), device: { system: 'Darwin', memory_gb: 24, supported: true }, models: ['0.8b', '2b', '8b'].map((id, index) => ({ id, weight_gb: [.98, 2.41, 6.07][index], compatible: true, available: true })), preparation: { stage: 'idle', model: null } } };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname === '/') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(dashboard); return; }
    if (url.pathname === '/text') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end('<!doctype html><meta charset="utf-8"><textarea id="draft">Draft text stays here.</textarea>'); return; }
    if (url.pathname === '/favicon.ico') { response.writeHead(204); response.end(); return; }
    let raw = ''; for await (const chunk of request) raw += chunk;
    const body = raw ? JSON.parse(raw) : null;
    const entry = { path: url.pathname, method: request.method, headers: request.headers, body, aborted: false };
    control.requests.push(entry);
    if (url.pathname === '/api/setup') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(control.setup)); return; }
    if (url.pathname === '/api/health') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ status: 'ok', app: 'plamo-for-cp', engine: { loaded_model: '2b', state: 'idle' } })); return; }
    if (url.pathname === '/api/translation-jobs') {
      if (control.status !== 200) { response.writeHead(control.status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ detail: 'モデルを管理画面でダウンロードしてください。' })); return; }
      const id = '00000000-0000-4000-8000-' + String(control.jobs.size + 1).padStart(12, '0');
      const job = { id, state: 'running', events: [], segments: body.segments, timers: [], previews: new Map(), completions: new Map() }; control.jobs.set(id, job);
      for (const segment of body.segments) for (const preview of control.jobPreviews[segment.id] || []) job.timers.push(setTimeout(() => { if (job.state === 'running' && !job.events.some(event => event.id === segment.id)) job.previews.set(segment.id, { id: segment.id, text: preview.text, revision: preview.revision }); }, preview.at));
      for (const segment of body.segments) {
        const complete = () => {
          if (job.state === 'cancelled' || job.events.some(event => event.id === segment.id)) return;
          job.completions.delete(segment.id);
          const error = control.jobErrors[segment.id] || (control.jobMarkerLimit && (segment.text.match(/\[\[PLM_\d+\]\]/g) || []).length > control.jobMarkerLimit ? '数式を保持できません。' : null);
          job.previews.delete(segment.id);
          job.events.push({ seq: job.events.length + 1, id: segment.id, ...(error ? { type: 'segment_error', message: error } : { type: 'translation', text: '日本語: ' + segment.text, cached: segment.id === '4' }) });
          if (job.events.length === job.segments.length) job.state = 'completed';
        };
        job.completions.set(segment.id, complete);
        if (!control.holdJobResults) job.timers.push(setTimeout(complete, control.jobDelays[segment.id] ?? control.delay));
      }
      const send = () => { if (!response.destroyed) { response.writeHead(202, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ id, state: 'queued', cursor: 0, total: body.segments.length })); } };
      if (control.creationDelay) job.timers.push(setTimeout(send, control.creationDelay)); else send();
      return;
    }
    const jobMatch = /^\/api\/translation-jobs\/([0-9a-f-]+)(\/cancel)?$/.exec(url.pathname);
    if (jobMatch) {
      const job = control.jobs.get(jobMatch[1]);
      if (!job) { response.writeHead(404); response.end('{}'); return; }
      if (jobMatch[2]) { if (job.state !== 'completed') { job.state = 'cancelled'; for (const timer of job.timers) clearTimeout(timer); } response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ id: job.id, state: job.state })); return; }
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ id: job.id, state: job.state, events: job.events.filter(event => event.seq > Number(url.searchParams.get('after'))), previews: [...job.previews.values()], cursor: job.events.length, completed: job.events.length, total: job.segments.length, cached: job.events.filter(event => event.cached).length }));
      return;
    }
    if (url.pathname === '/api/translate') {
      response.on('close', () => { if (!response.writableEnded) entry.aborted = true; });
      const timer = setTimeout(() => {
        if (response.destroyed) return;
        response.writeHead(control.status, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify(control.status === 200 ? { translations: body.segments.map(segment => ({ id: segment.id, text: '日本語: ' + segment.text })), cached: 0, model: body.model, precision: body.precision } : { detail: 'モデルを管理画面でダウンロードしてください。' }));
      }, control.delay);
      response.on('close', () => clearTimeout(timer));
      return;
    }
    response.writeHead(404, { 'Content-Type': 'application/json' }); response.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port, api = `http://127.0.0.1:${port}`;
  const output = join(directory, 'plamo-for-cp.user.js');
  await run(process.execPath, ['scripts/build_userscript.mjs', '--api-port', String(port), '--output', output]);
  const source = await readFile(output, 'utf8');
  const inflight = new Map();
  await context.exposeBinding('__gmChange', async ({ page }, key, value, deleted = false) => {
    if (deleted) delete control.values[key]; else control.values[key] = value;
    for (const other of context.pages()) if (other !== page && !other.isClosed()) await other.evaluate(({ key, value, deleted }) => window.__gmRemote?.(key, value, deleted), { key, value, deleted });
  });
  await context.exposeBinding('__gmOpen', (_source, url) => { control.opened.push(url); });
  await context.exposeBinding('__gmRequest', async ({ page }, id, details) => {
    const controller = new AbortController();
    if (!inflight.has(page)) inflight.set(page, new Map());
    inflight.get(page).set(id, controller);
    try {
      const response = await fetch(details.url, { method: details.method, headers: details.headers, body: details.data, redirect: 'error', signal: controller.signal });
      return { status: response.status, responseText: await response.text(), finalUrl: response.url };
    } finally { inflight.get(page).delete(id); }
  });
  await context.exposeBinding('__gmAbort', ({ page }, id) => { inflight.get(page)?.get(id)?.abort(); });
  async function page(url = 'https://codeforces.com/problemset/problem/1/A', html = problem) {
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    await page.addInitScript(({ values, source }) => {
      const listeners = new Map(); let nextListener = 1, nextRequest = 1;
      window.__gmValues = values; window.__gmMenus = {}; window.__gmReadKeys = [];
      window.GM_getValue = (key, fallback) => { window.__gmReadKeys.push(key); return Object.hasOwn(values, key) ? values[key] : fallback; };
      const notify = (key, old, value, remote) => { for (const listener of listeners.values()) if (listener.key === key) listener.callback(key, old, value, remote); };
      window.__gmRemote = (key, value, deleted) => { const old = values[key]; if (deleted) delete values[key]; else values[key] = value; notify(key, old, value, true); };
      window.GM_setValue = (key, value) => { const old = values[key]; values[key] = value; notify(key, old, value, false); window.__gmChange(key, value); };
      window.GM_deleteValue = key => { const old = values[key]; delete values[key]; notify(key, old, undefined, false); window.__gmChange(key, null, true); };
      window.GM_addValueChangeListener = (key, callback) => { const id = nextListener++; listeners.set(id, { key, callback }); return id; };
      window.GM_registerMenuCommand = (name, callback) => { window.__gmMenus[name] = callback; };
      window.GM_openInTab = url => window.__gmOpen(url);
      window.GM_xmlhttpRequest = details => {
        const id = nextRequest++; let finished = false;
        window.__gmRequest(id, { url: details.url, method: details.method || 'GET', data: details.data, headers: details.headers }).then(result => { if (!finished) { finished = true; details.onload?.(result); } }).catch(() => { if (!finished) { finished = true; details.onerror?.(); } });
        return { abort() { if (finished) return; finished = true; window.__gmAbort(id); details.onabort?.(); } };
      };
      // The generated artifact itself executes, including its GM transport shim.
      (0, eval)(source);
    }, { values: structuredClone(control.values), source });
    if (!url.startsWith(api)) await page.route(url, route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }));
    await page.goto(url);
    return page;
  }
  async function close() {
    for (const job of control.jobs.values()) for (const timer of job.timers) clearTimeout(timer);
    await context.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
  return { context, control, source, page, api, close };
}

test('generated Tampermonkey metadata limits GM privileges', async () => {
  const harness = await fixture();
  try {
    assert.match(harness.source, /@grant\s+GM_xmlhttpRequest/);
    assert.match(harness.source, /@connect\s+127\.0\.0\.1/);
    assert.doesNotMatch(harness.source, /@grant\s+(?:unsafeWindow|GM_cookie)|@connect\s+\*/);
  } finally { await harness.close(); }
});

test('page-authored toolbar events cannot invoke GM actions or alter cross-site preferences', async () => {
  const harness = await fixture({ 'cp-translator-model': '2b' });
  try {
    const page = await harness.page();
    await page.getByLabel('モデルサイズ').waitFor();
    await page.evaluate(() => {
      const shadow = document.querySelector('#cp-translator').shadowRoot;
      shadow.querySelector('[aria-label="日本語に翻訳"]').click();
      shadow.querySelector('[aria-label="設定・管理"]').click();
      const model = shadow.querySelector('select'); model.value = '8b'; model.dispatchEvent(new Event('change'));
      const grip = shadow.querySelector('.grip');
      grip.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home' }));
      grip.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
      grip.dispatchEvent(new PointerEvent('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 }));
      grip.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientX: 40, clientY: 40 }));
      grip.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }));
    });
    await page.waitForTimeout(150);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    assert.deepEqual(harness.control.opened, []);
    assert.equal(harness.control.values['cp-translator-model'], '2b');
    assert.equal(harness.control.values['cp-translator-panel-offset'], undefined);
    assert.equal(harness.control.values['cp-translator-panel-position'], undefined);
    assert.equal(await page.getByLabel('モデルサイズ').inputValue(), '2b');
    await chooseModel(page, '8b');
    await page.getByRole('button', { name: '翻訳バーを移動', exact: true }).press('ArrowLeft');
    await page.getByRole('button', { name: '設定・管理', exact: true }).click();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(harness.control.values['cp-translator-model'], '8b');
    assert.equal(typeof harness.control.values['cp-translator-panel-offset'].x, 'number');
    assert.deepEqual(harness.control.opened, [harness.api + '/#browser-settings']);
    assert.equal(harness.control.requests.find(request => request.body)?.body.model, '8b');
  } finally { await harness.close(); }
});

test('generated GM jobs display early completions, refill before the first job ends, and keep original-view buffering safe', async () => {
  const harness = await fixture({ 'cp-translator-model': '2b', 'cp-translator-batch-size': 4 });
  harness.control.setup.supported_translation_jobs = true;
  harness.control.jobDelays = { 0: 10, 1: 1000, 2: 10, 3: 20, 4: 1200, 5: 10 };
  harness.control.jobErrors = { 3: 'この段落は原文を保持します。' };
  try {
    const html = '<div class="problem-statement">' + Array.from({ length: 6 }, (_, index) => '<p id="p' + index + '">Paragraph ' + index + ' <var>n_' + index + '</var>.</p>').join('') + '<pre>1  2\n3</pre></div>';
    const page = await harness.page('https://codeforces.com/problemset/problem/71/A', html);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#p0').textContent.startsWith('日本語'));
    assert.equal(await page.locator('#p1').textContent(), 'Paragraph 1 n_1.');
    await page.waitForFunction(() => document.querySelector('#p2').textContent.startsWith('日本語'));
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('5 箇所を翻訳しました。1 箇所は失敗'));
    assert.ok((await page.locator('.problem-statement p').allTextContents()).every(text => text.startsWith('Paragraph')));
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    assert.match(await page.locator('#p5').textContent(), /日本語/);
    assert.equal(await page.locator('#p3').textContent(), 'Paragraph 3 n_3.');
    assert.deepEqual(harness.control.requests.filter(request => request.path === '/api/translation-jobs').map(request => request.body.segments.length), [1, 1, 1, 1, 1, 1]);
    assert.equal(await page.locator('pre').textContent(), '1  2\n3');
  } finally { await harness.close(); }
});

test('Stop retains a pending GM creation response and cancels its returned job without late DOM changes', async () => {
  const harness = await fixture();
  harness.control.setup.supported_translation_jobs = true;
  harness.control.creationDelay = 180; harness.control.delay = 1000;
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/72/A', '<div class="problem-statement"><p id="p">Original problem paragraph.</p></div>');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    const deadline = Date.now() + 2000;
    while (!harness.control.jobs.size) { assert.ok(Date.now() < deadline); await new Promise(resolve => setTimeout(resolve, 5)); }
    await page.getByRole('button', { name: '停止', exact: true }).click();
    while (![...harness.control.jobs.values()].every(job => job.state === 'cancelled')) { assert.ok(Date.now() < deadline); await new Promise(resolve => setTimeout(resolve, 5)); }
    assert.equal(await page.locator('#p').textContent(), 'Original problem paragraph.');
    assert.ok(harness.control.requests.some(request => request.path.endsWith('/cancel') && request.headers['x-plamo-client'] === 'userscript'));
    assert.ok(!harness.control.requests.some(request => request.path === '/api/translate'));
  } finally { await harness.close(); }
});

test('Windows full capabilities stream canonical GM previews and buffer verified results in original view', async () => {
  const harness = await fixture({ 'cp-translator-batch-size': 32 });
  Object.assign(harness.control.setup, { supported_translation_jobs: true, supported_translation_previews: true, device: { system: 'Windows', memory_gb: 24, supported: true } });
  harness.control.holdJobResults = true;
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/73/A', '<div class="problem-statement"><p id="p">Find <strong>the answer</strong> for <var id="v">n</var>.</p></div>');
    const original = await page.locator('#p').textContent(), math = await page.locator('#v').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await waitForMock(() => harness.control.jobs.size === 1);
    const job = [...harness.control.jobs.values()][0];
    job.previews.set('0', { id: '0', revision: 1, text: '<plm-a>途中<img src=x onerror=alert(1)>[[PLM_' });
    await page.locator('#p [data-cp-translator-preview]').waitFor();
    assert.equal(harness.control.values['cp-translator-translation-previews'], true);
    assert.equal(harness.control.requests.find(request => request.path === '/api/translation-jobs').body.batch_size, 32);
    assert.match(await page.locator('#p strong').textContent(), /途中/);
    assert.equal(await page.locator('#p img').count(), 0);
    assert.doesNotMatch(await page.locator('#p').textContent(), /PLM_/);
    job.previews.set('0', { id: '0', revision: 2, text: '<plm-a>改訂された途中</plm-a> [[PLM_0]]' });
    await page.waitForFunction(() => document.querySelector('#p strong').textContent === '改訂された途中');
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    job.completions.get('0')();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(await page.locator('#p').textContent(), original);
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    assert.match(await page.locator('#p').textContent(), /^日本語/);
    assert.equal(await page.locator('#p [data-cp-translator-preview]').count(), 0);
    assert.equal(await page.locator('#v').evaluate(node => node.outerHTML), math);
  } finally { await harness.close(); }
});

test('Windows parallel GM jobs stop pending previews while retaining completed paragraphs and original DOM', async () => {
  const harness = await fixture({ 'cp-translator-batch-size': 32 });
  Object.assign(harness.control.setup, { supported_translation_jobs: true, supported_translation_previews: true, device: { system: 'Windows', memory_gb: 24, supported: true } });
  harness.control.holdJobResults = true;
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/74/A', '<div class="problem-statement"><p id="done">First paragraph.</p><p id="pending">Second <var>n</var> paragraph.</p></div>');
    const original = await page.locator('#pending').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await waitForMock(() => harness.control.jobs.size === 2);
    const jobs = [...harness.control.jobs.values()];
    assert.ok(jobs.every(job => job.state === 'running'), 'both Windows jobs are admitted before either finishes');
    jobs.find(job => job.completions.has('0')).completions.get('0')();
    jobs.find(job => job.completions.has('1')).previews.set('1', { id: '1', revision: 1, text: '途中 [[PLM_0]]' });
    await page.locator('#pending [data-cp-translator-preview]').waitFor();
    await page.waitForFunction(() => document.querySelector('#done').textContent.startsWith('日本語'));
    await page.getByRole('button', { name: '停止', exact: true }).click();
    assert.equal(await page.locator('#pending').evaluate(node => node.outerHTML), original);
    assert.match(await page.locator('#done').textContent(), /^日本語/);
    await waitForMock(() => jobs.every(job => job.state !== 'running'));
    assert.ok(harness.control.requests.filter(request => request.path === '/api/translation-jobs').every(request => request.body.batch_size === 32));
    assert.equal(harness.control.values['cp-translator-batch-size'], 32);
    assert.equal(await page.locator('[data-cp-translator-preview]').count(), 0);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#done').textContent(), 'First paragraph.');
  } finally { await harness.close(); }
});

test('formatted whole-sentence retries stream canonical previews, buffer in original view, and roll back only unfinished work on Stop', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true;
  harness.control.holdJobResults = true; harness.control.jobMarkerLimit = 1;
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '<plm-a>すべて</plm-a>の丸記号[[PLM_0]]' }];
  harness.control.jobPreviews[1] = [{ at: 10, revision: 1, text: '<plm-a>すべて</plm-a>の丸記号[[PLM_0]]' }];
  harness.control.jobPreviews[2] = [{ at: 10, revision: 1, text: '<plm-b>すべて</plm-b>の角記号[[PLM_1]]' }];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/2196/D', '<div class="problem-statement"><p id="p">Round <strong>all</strong> symbols use <var>n</var>. Square <em>all</em> symbols use <var>m</var>.</p></div>');
    const original = await page.locator('#p').textContent();
    await page.locator('#p').evaluate(node => { window.__formattedRetryOriginal = [...node.childNodes]; });
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#p [data-cp-translator-preview]').waitFor();
    [...harness.control.jobs.values()][0].completions.get('0')();
    await page.waitForFunction(() => document.querySelectorAll('#p [data-cp-translator-preview]').length === 2);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    const firstRetry = [...harness.control.jobs.values()].find(job => job.state === 'running' && job.segments[0].text.startsWith('Round'));
    firstRetry.completions.get(firstRetry.segments[0].id)();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.count').textContent === '2/3');
    assert.equal(await page.locator('#p').textContent(), original);
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    assert.match(await page.locator('#p').textContent(), /^日本語:/);
    assert.equal(await page.locator('#p [data-cp-translator-preview]').count(), 1);
    await page.getByRole('button', { name: '停止', exact: true }).click();
    const stopped = await page.locator('#p').textContent();
    assert.match(stopped, /Square all symbols use m\.$/);
    assert.equal(await page.locator('#p [data-cp-translator-preview]').count(), 0);
    const sent = harness.control.requests.filter(request => request.path === '/api/translation-jobs').flatMap(request => request.body.segments.map(segment => segment.text));
    assert.deepEqual(sent, ['Round <plm-a>all</plm-a> symbols use [[PLM_0]]. Square <plm-b>all</plm-b> symbols use [[PLM_1]].', 'Round <plm-a>all</plm-a> symbols use [[PLM_0]]. ', 'Square <plm-b>all</plm-b> symbols use [[PLM_1]].']);
    await page.waitForTimeout(250); assert.equal(await page.locator('#p').textContent(), stopped);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__formattedRetryOriginal[index])), true);
  } finally { await harness.close(); }
});

test('sentence retries run before a held slow primary finishes and ahead of unsubmitted paragraphs', async () => {
  const harness = await fixture({ 'cp-translator-batch-size': 2 }); harness.control.setup.supported_translation_jobs = true;
  harness.control.holdJobResults = true; harness.control.jobMarkerLimit = 1;
  const jobFor = id => [...harness.control.jobs.values()].find(job => job.segments.some(segment => segment.id === id));
  const waitForJob = async id => { const deadline = Date.now() + 5000; while (!jobFor(id)) { if (Date.now() >= deadline) throw new Error('Retry job was not admitted: ' + id); await new Promise(resolve => setTimeout(resolve, 10)); } return jobFor(id); };
  try {
    const page = await harness.page('https://codeforces.com/contest/2196/problem/D', '<div class="problem-statement"><p id="retry">Round <strong>all</strong> symbols use <var>n</var>. Square symbols use <var>m</var>.</p><p id="slow">A slow primary paragraph.</p><p>A later primary paragraph.</p></div>');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    const failed = await waitForJob('0'), slow = await waitForJob('1');
    failed.completions.get('0')();
    const first = await waitForJob('3');
    assert.equal(slow.events.length, 0, 'the failed paragraph must not wait for the slow primary');
    assert.equal(jobFor('2'), undefined, 'a sentence retry has priority over an unsubmitted primary');
    first.completions.get('3')();
    const second = await waitForJob('4'); second.completions.get('4')();
    await page.waitForFunction(() => (document.querySelector('#retry').textContent.match(/日本語:/g) || []).length === 2);
    assert.equal(await page.locator('#slow').textContent(), 'A slow primary paragraph.');
    const later = await waitForJob('2'); later.completions.get('2')(); slow.completions.get('1')();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const sent = harness.control.requests.filter(request => request.path === '/api/translation-jobs').flatMap(request => request.body.segments);
    assert.deepEqual(sent.map(segment => segment.id), ['0', '1', '3', '4', '2']);
    assert.equal(sent[2].text + sent[3].text, sent[0].text);
  } finally { await harness.close(); }
});

test('Stop during a second-level retry preview preserves the successful half and exact original view', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true;
  harness.control.holdJobResults = true; harness.control.jobMarkerLimit = 3; harness.control.jobErrors[2] = 'この半分は文単位で再試行してください。';
  harness.control.jobPreviews[3] = [{ at: 10, revision: 1, text: '途中 [[PLM_3]]' }];
  const jobFor = id => [...harness.control.jobs.values()].find(job => job.segments.some(segment => segment.id === id));
  const waitForJob = async id => { const deadline = Date.now() + 5000; while (!jobFor(id)) { if (Date.now() >= deadline) throw new Error('Retry job was not admitted: ' + id); await new Promise(resolve => setTimeout(resolve, 10)); } return jobFor(id); };
  try {
    const names = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot'];
    const page = await harness.page('https://codeforces.com/contest/2196/problem/D', `<div class="problem-statement"><p id="p">${names.map((name, index) => `${name} case uses <var>${index}</var>.`).join(' ')}</p></div>`);
    const original = await page.locator('#p').textContent();
    await page.locator('#p').evaluate(node => { window.__hierarchyOriginal = [...node.childNodes]; });
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    (await waitForJob('0')).completions.get('0')();
    const left = await waitForJob('1'), right = await waitForJob('2');
    left.completions.get('1')(); right.completions.get('2')();
    await page.locator('#p [data-cp-translator-preview]').waitFor();
    assert.match(await page.locator('#p').textContent(), /^日本語:/);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    await page.getByRole('button', { name: '停止', exact: true }).click();
    assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__hierarchyOriginal[index])), true);
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    const stopped = await page.locator('#p').textContent();
    assert.equal((stopped.match(/日本語:/g) || []).length, 1);
    assert.match(stopped, /Delta case uses 3\. Echo case uses 4\. Foxtrot case uses 5\.$/);
    assert.equal(await page.locator('[data-cp-translator-preview]').count(), 0);
    const sent = harness.control.requests.filter(request => request.path === '/api/translation-jobs').flatMap(request => request.body.segments);
    assert.equal(sent.filter(segment => segment.id === '1').length, 1);
    assert.equal(sent.find(segment => segment.id === '1').text + sent.find(segment => segment.id === '2').text, sent.find(segment => segment.id === '0').text);
    assert.equal(sent.filter(segment => ['3', '4', '5'].includes(segment.id)).map(segment => segment.text).join(''), sent.find(segment => segment.id === '2').text);
    await page.waitForTimeout(250); assert.equal(await page.locator('#p').textContent(), stopped);
  } finally { await harness.close(); }
});

test('a rejected final translation removes its preview and leaves original math unchanged', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true;
  // Hold the final event until the draft is observed. A delayed creation or a
  // busy browser can otherwise miss a valid, short-lived preview completely.
  harness.control.holdJobResults = true; harness.control.creationDelay = 800;
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '途中 [[PLM_0]]' }]; harness.control.jobErrors[0] = '数式を保持できません。';
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/75/A', '<div class="problem-statement"><p id="p">Find <var>n</var> integers.</p></div>');
    const original = await page.locator('#p').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#p [data-cp-translator-preview]').waitFor();
    assert.equal([...harness.control.jobs.values()][0].events.length, 0);
    [...harness.control.jobs.values()][0].completions.get('0')();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('失敗'));
    assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), original);
    assert.equal(await page.locator('[data-cp-translator-preview]').count(), 0);
  } finally { await harness.close(); }
});

test('unknown or duplicated preview math slots never replace source and a later strict final result can still apply', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true; harness.control.delay = 900;
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '危険[[PLM_99]]' }, { at: 300, revision: 2, text: '危険[[PLM_0]][[PLM_0]]' }];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/76/A', '<div class="problem-statement"><p id="p">Find <var>n</var> integers.</p></div>');
    const original = await page.locator('#p').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForTimeout(550);
    assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), original);
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.match(await page.locator('#p').textContent(), /^日本語/);
    assert.equal(await page.locator('var').textContent(), 'n');
  } finally { await harness.close(); }
});

test('page edits to an inline preview reject a final result and remove its draft decoration without overwriting the edit', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true;
  harness.control.holdJobResults = true;
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '途中 [[PLM_0]]' }];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/77/A', '<div class="problem-statement"><p id="p">Find <var>n</var> integers.</p></div>');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#p [data-cp-translator-preview]').waitFor();
    await page.locator('#p [data-cp-translator-preview]').evaluate(node => { node.firstChild.textContent = 'Page-authored replacement.'; });
    const job = [...harness.control.jobs.values()][0];
    assert.equal(job.events.length, 0);
    job.completions.get('0')();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('ページの内容が更新'));
    assert.match(await page.locator('#p').textContent(), /Page-authored replacement/);
    assert.equal(await page.locator('[data-cp-translator-preview]').count(), 0);
    await page.waitForTimeout(250);
    assert.match(await page.locator('#p').textContent(), /Page-authored replacement/);
  } finally { await harness.close(); }
});

test('arbitrary pages stay idle until the Tampermonkey menu is clicked, then use only the local userscript transport', async () => {
  const harness = await fixture();
  try {
    const page = await harness.page('https://example.org/article', article);
    assert.deepEqual(await page.evaluate(() => Object.keys(window.__gmMenus)), ['このページを翻訳', 'テキストを翻訳', '設定・管理']);
    assert.equal(harness.control.requests.length, 0);
    assert.equal(await page.locator('#cp-translator').count(), 0);
    await page.evaluate(() => window.__gmMenus['このページを翻訳']());
    await page.waitForFunction(() => document.querySelector('#cp-translator')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.match(await page.locator('#p').textContent(), /日本語/);
    assert.ok(harness.control.requests.every(request => request.headers['x-plamo-client'] === 'userscript'));
    assert.ok(harness.control.requests.every(request => ['/api/setup', '/api/health', '/api/translate'].includes(request.path)));
    assert.equal(harness.control.requests.find(request => request.body)?.body.document_type, 'general');
    await page.getByRole('button', { name: '設定・管理', exact: true }).click();
    await page.waitForFunction(() => true);
    assert.deepEqual(harness.control.opened, [harness.api + '/#browser-settings']);
  } finally { await harness.close(); }
});

test('the always-available text menu opens the local workspace without preparing or translating anything', async () => {
  const harness = await fixture();
  try {
    const page = await harness.page('https://example.org/article', article);
    await page.evaluate(() => window.__gmMenus['テキストを翻訳']());
    await page.waitForFunction(() => true);
    assert.deepEqual(harness.control.opened, [harness.api + '/text']);
    assert.equal(harness.control.requests.length, 0);
    assert.equal(await page.locator('#cp-translator').count(), 0);
  } finally { await harness.close(); }
});

test('known judge bootstrap defaults to2B and never auto-translates or downloads', async () => {
  const harness = await fixture({ 'cp-translator-model': '31b' });
  try {
    const page = await harness.page();
    await page.getByLabel('モデルサイズ').waitFor();
    assert.equal(await page.getByLabel('モデルサイズ').inputValue(), '2b');
    assert.equal(await page.getByLabel('モデルサイズ').locator('option[value="31b"]').count(), 0);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    await chooseModel(page, '0.8b');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    const reopened = await harness.page();
    await reopened.getByLabel('モデルサイズ').waitFor();
    assert.equal(await reopened.getByLabel('モデルサイズ').inputValue(), '0.8b', 'a saved explicit model choice overrides the new default');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(harness.control.requests.find(request => request.body)?.body.model, '0.8b');
  } finally { await harness.close(); }
});

test('stop aborts a live GM request and leaves untranslated source intact', async () => {
  const harness = await fixture();
  try {
    harness.control.delay = 2000;
    const page = await harness.page();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('翻訳中'));
    for (let attempt = 0; attempt < 50 && !harness.control.requests.some(request => request.body); attempt++) await new Promise(resolve => setTimeout(resolve, 10));
    await page.getByRole('button', { name: '停止', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('停止'));
    assert.equal(await page.locator('#p').textContent(), 'You are given positive integers. Find the answer.');
    for (let attempt = 0; attempt < 50 && !harness.control.requests.some(request => request.aborted); attempt++) await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(harness.control.requests.some(request => request.aborted));
  } finally { await harness.close(); }
});

test('missing model failure points to management and never calls model preparation', async () => {
  const harness = await fixture();
  try {
    harness.control.status = 409;
    const page = await harness.page();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('ダウンロード'));
    assert.equal(await page.locator('#p').textContent(), 'You are given positive integers. Find the answer.');
    assert.ok(harness.control.requests.every(request => !/prepare|download/.test(request.path)));
  } finally { await harness.close(); }
});

test('local dashboard injects always-visible settings, persists custom domains/glossary, and syncs ready models across tabs', async () => {
  const harness = await fixture();
  try {
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    const page = await harness.page(harness.api + '/');
    await page.getByRole('heading', { name: 'このブラウザーの翻訳設定' }).waitFor();
    assert.equal(await page.getByRole('region', { name: 'このブラウザーの翻訳設定', exact: true }).count(), 1);
    assert.equal(await page.locator('html').getAttribute('data-cp-translator-userscript'), 'ready');
    assert.equal(await page.locator('#browser-settings > [data-cp-translator-ui]').getAttribute('data-cp-translator-userscript-version'), harness.source.match(/@version\s+(\S+)/)[1]);
    assert.equal(await page.locator('#browser-settings details').count(), 0);
    assert.equal(await page.getByLabel('ページを開いたら自動で翻訳').count(), 0);
    assert.equal(await page.getByLabel('周辺の原文を参考にする').count(), 0);
    assert.equal(await page.getByLabel('用語集', { exact: true }).inputValue(), defaultGlossary.map(pair => pair.source + ' = ' + pair.target).join('\n'));
    assert.equal(await page.getByLabel('用語集', { exact: true }).getAttribute('rows'), '12');
    const batchSelector = page.getByLabel('翻訳の並列数', { exact: true });
    assert.equal(await batchSelector.count(), 1);
    assert.equal(await batchSelector.inputValue(), '16');
    assert.deepEqual(await batchSelector.locator('option').evaluateAll(options => options.map(option => option.value)), ['1', '2', '3', '4', '5', '6', '8', '10', '12', '16', '20', '24', '32']);
    assert.deepEqual(await batchSelector.locator('option').allTextContents(), ['1', '2', '3', '4', '5', '6', '8', '10', '12', '16', '20', '24', '32']);
    assert.deepEqual(harness.control.values['cp-translator-supported-batch-sizes'], [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32]);
    await batchSelector.selectOption('4');
    for (const invalid of ['0', '7', '33', '1.5']) {
      await batchSelector.evaluate((select, value) => { select.add(new Option(value, value)); select.value = value; select.dispatchEvent(new Event('change')); }, invalid);
      assert.equal(await batchSelector.inputValue(), '4');
      assert.equal(await page.evaluate(() => window.__gmValues['cp-translator-batch-size']), 4);
    }
    for (const name of ['Codeforces', 'CodeChef', 'CSES', 'Kattis', 'Repovive', 'UOJ', 'TLX', 'Project Euler', 'arXiv']) {
      assert.equal(await page.getByLabel(`${name} の表示`, { exact: true }).isVisible(), true);
      assert.equal(await page.getByLabel(`${name} の自動翻訳`, { exact: true }).isChecked(), false);
    }
    assert.deepEqual(await page.locator('#sites > tr > td:first-child > a').allTextContents(), ['Codeforces', 'CodeChef', 'CSES', 'Kattis', 'Repovive', 'UOJ', 'TLX', 'Project Euler', 'arXiv']);
    assert.equal(await page.getByLabel('SPOJ の表示', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('DMOJ の表示', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('翻訳バーの位置', { exact: true }).count(), 0);
    const siteLink = page.getByRole('link', { name: 'Codeforces', exact: true });
    assert.equal(await siteLink.getAttribute('href'), 'https://codeforces.com/');
    assert.equal(await siteLink.getAttribute('target'), '_blank');
    await siteLink.evaluate(link => link.addEventListener('click', event => event.preventDefault()));
    await siteLink.click();
    assert.equal(await page.getByLabel('Codeforces の表示', { exact: true }).isChecked(), true);
    await page.getByLabel('サイトを追加', { exact: true }).fill('example.org');
    await page.getByRole('button', { name: '追加', exact: true }).click();
    await page.getByText('サイトを保存しました。対象のページを再読み込みしてください。', { exact: true }).waitFor();
    assert.equal(harness.control.values['cp-translator-custom-sites'][0].pattern, 'https://example.org/*');
    assert.equal(await page.getByRole('link', { name: 'https://example.org/*', exact: true }).getAttribute('href'), 'https://example.org/');
    const site = page.locator('.custom-site');
    await site.getByRole('button', { name: /を編集/ }).click();
    await page.getByLabel('サイトを追加', { exact: true }).fill('https://example.org/articles/*');
    await page.getByRole('button', { name: '変更を保存', exact: true }).click();
    await page.getByLabel('用語集', { exact: true }).fill('array = 配列\nedge = 辺');
    await page.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await page.getByText('用語集を保存しました（2件）。', { exact: true }).waitFor();
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('cp-translator-model-selected', { detail: { model: '2b' } })));
    await judge.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('select').value === '2b');
    assert.deepEqual(harness.control.values['cp-translator-glossary'], [{ source: 'array', target: '配列' }, { source: 'edge', target: '辺' }]);
    assert.equal(harness.control.values['cp-translator-model'], '2b');
    assert.equal(harness.control.values['cp-translator-batch-size'], 4);
    await site.getByRole('button', { name: /を削除/ }).click();
    await page.getByText('サイトを削除しました。', { exact: true }).waitFor();
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], []);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('the management reset button restores saved GM preferences across tabs without clearing capabilities or contacting model APIs', async () => {
  const harness = await fixture({
    'cp-translator-model': '8b', 'cp-translator-pending-model': '2b',
    'cp-translator-disabled-sites': ['codechef'], 'cp-translator-auto-sites': { kattis: true, custom: true },
    'cp-translator-custom-sites': [{ id: 'custom', pattern: 'https://example.org/*', enabled: true }],
    'cp-translator-glossary': [{ source: 'array', target: '配列' }],
    'cp-translator-panel-position': 'left-top', 'cp-translator-panel-offset': { x: .2, y: .3 },
    'cp-translator-batch-size': 3,
  });
  try {
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    assert.equal(await judge.getByLabel('モデルサイズ').inputValue(), '8b');
    const page = await harness.page(harness.api + '/');
    const reset = page.getByRole('button', { name: '設定をデフォルトに戻す', exact: true });
    await reset.waitFor();
    assert.equal(await reset.getAttribute('id'), 'reset-browser-settings');
    await page.evaluate(() => {
      localStorage.setItem('plamo-text-prefetch', JSON.stringify({ '0.8b': 16, '2b': 16, '8b': 16 }));
      localStorage.setItem('plamo-pdf-prefetch', JSON.stringify({ '0.8b': 8 }));
      localStorage.setItem('plamo-text-use-context', 'true');
      localStorage.setItem('unrelated-origin-setting', 'keep');
    });
    const derived = await page.evaluate(() => Object.fromEntries(['cp-translator-supported-batch-sizes', 'cp-translator-model-weights', 'cp-translator-translation-jobs', 'cp-translator-translation-previews'].map(key => [key, window.__gmValues[key]])));
    await page.getByLabel('用語集', { exact: true }).fill('unsaved = 下書き');
    await reset.click();
    await page.getByText('ブラウザーの設定をデフォルトに戻しました。', { exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => ['plamo-text-prefetch', 'plamo-pdf-prefetch', 'plamo-text-use-context', 'cp-translator-batch-size', 'unrelated-origin-setting'].map(key => localStorage.getItem(key))), [JSON.stringify({ '0.8b': 16, '2b': 16, '8b': 16 }), JSON.stringify({ '0.8b': 8 }), 'true', '16', 'keep']);
    await judge.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('select').value === '2b' && document.querySelector('#cp-translator').style.right === '14px');
    const values = await page.evaluate(() => window.__gmValues);
    for (const [key, expected] of Object.entries({
      'cp-translator-model': '2b', 'cp-translator-disabled-sites': [], 'cp-translator-auto-sites': {}, 'cp-translator-custom-sites': [],
      'cp-translator-glossary': defaultGlossary,
      'cp-translator-batch-size': 16,
      'cp-translator-panel-position': 'right-bottom', 'cp-translator-panel-offset': null,
    })) assert.deepEqual(values[key], expected, key);
    assert.equal(Object.hasOwn(values, 'cp-translator-pending-model'), false);
    for (const [key, value] of Object.entries(derived)) assert.deepEqual(values[key], value, key);
    assert.equal(await page.getByLabel('用語集', { exact: true }).inputValue(), defaultGlossary.map(pair => pair.source + ' = ' + pair.target).join('\n'));
    assert.equal(await page.locator('.custom-site').count(), 0);
    assert.equal(await page.getByLabel('周辺の原文を参考にする').count(), 0);
    for (const name of ['Codeforces', 'CodeChef', 'CSES', 'Kattis', 'Repovive', 'UOJ', 'TLX', 'Project Euler', 'arXiv']) {
      assert.equal(await page.getByLabel(`${name} の表示`, { exact: true }).isChecked(), true);
      assert.equal(await page.getByLabel(`${name} の自動翻訳`, { exact: true }).isChecked(), false);
    }
    // A new tab reads the persisted GM store, independently of this tab's DOM.
    const reopened = await harness.page(harness.api + '/');
    await reopened.getByRole('button', { name: '設定をデフォルトに戻す', exact: true }).waitFor();
    assert.equal(await reopened.getByLabel('翻訳の並列数', { exact: true }).inputValue(), '16');
    assert.equal(await page.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.equal(await judge.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'), 'reset must not prepare/delete models, clear the cache or translate');
  } finally { await harness.close(); }
});

test('saved glossary entries remain authoritative, while clear stays empty and general prose uses custom terms', async () => {
  const harness = await fixture({ 'cp-translator-glossary': [{ source: 'JURY', target: '審査員' }, { source: 'tree', target: '木' }] });
  try {
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('用語集', { exact: true }).waitFor();
    const expected = [{ source: 'JURY', target: '審査員' }, { source: 'tree', target: '木' }];
    assert.equal(await manager.getByLabel('用語集', { exact: true }).inputValue(), expected.map(pair => pair.source + ' = ' + pair.target).join('\n'));
    assert.deepEqual(harness.control.values['cp-translator-glossary'], expected);
    const judge = await harness.page();
    await judge.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await judge.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.deepEqual(harness.control.requests.find(request => request.body)?.body.glossary, expected);
    assert.equal(await judge.evaluate(() => localStorage.getItem('cp-translator-glossary')), null, 'site origin must not receive the native glossary mirror');
    const general = await harness.page('https://example.org/article', article);
    const before = harness.control.requests.length;
    await general.evaluate(() => window.__gmMenus['このページを翻訳']());
    assert.ok(harness.control.requests.slice(before).filter(request => request.body).every(request => JSON.stringify(request.body.glossary) === JSON.stringify([{ source: 'JURY', target: '審査員' }, { source: 'tree', target: '木' }])));
    await manager.getByLabel('用語集', { exact: true }).fill('');
    await manager.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await manager.getByText('用語集を保存しました（0件）。', { exact: true }).waitFor();
    await judge.waitForFunction(() => window.__gmValues['cp-translator-glossary']?.length === 0);
    const clearedBefore = harness.control.requests.length;
    await judge.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await judge.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const sent = harness.control.requests.slice(clearedBefore).filter(request => request.body);
    assert.ok(sent.length > 0 && sent.every(request => Array.isArray(request.body.glossary) && request.body.glossary.length === 0));
    const reopened = await harness.page(harness.api + '/');
    await reopened.getByLabel('用語集', { exact: true }).waitFor();
    assert.equal(await reopened.getByLabel('用語集', { exact: true }).inputValue(), '');
    assert.deepEqual(harness.control.values['cp-translator-glossary'], []);
  } finally { await harness.close(); }
});

test('the trusted text workspace mirrors glossary updates and clears without changing its draft', async () => {
  const saved = [{ source: 'tree', target: '木' }];
  const harness = await fixture({ 'cp-translator-glossary': saved });
  try {
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('用語集', { exact: true }).waitFor();
    assert.deepEqual(harness.control.values['cp-translator-glossary'], saved);
    const text = await harness.page(harness.api + '/text');
    await text.waitForFunction(count => JSON.parse(localStorage.getItem('cp-translator-glossary') || 'null')?.length === count, saved.length);
    await text.locator('#draft').fill('Keep this unfinished text.');
    await text.evaluate(() => { window.__glossaryChanges = 0; addEventListener('storage', event => { if (event.key === 'cp-translator-glossary') window.__glossaryChanges++; }); });
    await manager.getByLabel('用語集', { exact: true }).fill('');
    await manager.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await text.waitForFunction(() => localStorage.getItem('cp-translator-glossary') === '[]' && window.__glossaryChanges > 0);
    assert.equal(await text.locator('#draft').inputValue(), 'Keep this unfinished text.');
    await manager.getByRole('button', { name: '設定をデフォルトに戻す', exact: true }).click();
    await text.waitForFunction(count => JSON.parse(localStorage.getItem('cp-translator-glossary'))?.length === count, defaultGlossary.length);
    assert.deepEqual(await text.evaluate(() => JSON.parse(localStorage.getItem('cp-translator-glossary'))), defaultGlossary);
    assert.equal(await text.locator('#draft').inputValue(), 'Keep this unfinished text.');
    assert.equal(await text.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('generated transport shares a common batch choice across models and preserves it on a constrained device', async () => {
  const harness = await fixture({ 'cp-translator-model': '2b', 'cp-translator-batch-size': 32 });
  try {
    const page = await harness.page();
    for (const model of ['0.8b', '2b', '8b']) {
      await chooseModel(page, model);
      await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    }
    const normal = harness.control.requests.filter(request => request.body);
    assert.equal(normal.length, 3);
    assert.ok(normal.every(request => request.body.batch_size === 32 && request.body.segments.length === 2));
    assert.ok(normal.every(request => request.body.source_context === '' && !Object.hasOwn(request.body, 'history')));
    harness.control.setup.supported_batch_sizes = [1];
    const manager = await harness.page(harness.api + '/');
    const select = manager.getByLabel('翻訳の並列数', { exact: true });
    await select.waitFor();
    assert.equal(await select.inputValue(), '1');
    assert.equal(await select.locator('option[value="32"]').isDisabled(), true);
    assert.equal(await manager.evaluate(() => localStorage.getItem('cp-translator-batch-size')), '32');
    const constrained = await harness.page();
    await constrained.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await constrained.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.ok(harness.control.requests.filter(request => request.body).slice(normal.length).every(request => request.body.batch_size === 1 && request.body.segments.length === 1));
    assert.equal(harness.control.values['cp-translator-batch-size'], 32);
  } finally { await harness.close(); }
});

test('Windows capability refresh restores parallel jobs and600-character chunks without replacing the saved choice', async () => {
  const harness = await fixture({ 'cp-translator-batch-size': 32 });
  Object.assign(harness.control.setup, { supported_translation_jobs: true, supported_translation_previews: false, supported_batch_sizes: [1], device: { system: 'Windows', memory_gb: 24, supported: true } });
  const sentence = 'This sentence explains the complete strategy and the conditions that must be met before any decision is made, while retaining all relevant details from previous rounds. ';
  const source = sentence.repeat(6);
  const submitted = () => harness.control.requests.filter(request => request.path === '/api/translation-jobs');
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/75/A', '<div class="problem-statement"><p id="p">' + source + '</p></div>');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const sequential = submitted();
    assert.equal(sequential.flatMap(request => request.body.segments).length, 6);
    assert.ok(sequential.every(request => request.body.batch_size === 1));
    assert.equal(sequential.flatMap(request => request.body.segments.map(segment => segment.text)).join(''), source);
    assert.equal(harness.control.values['cp-translator-batch-size'], 32);
    assert.equal(harness.control.values['cp-translator-translation-previews'], false);
    const limitedManager = await harness.page(harness.api + '/');
    await limitedManager.getByLabel('翻訳の並列数', { exact: true }).waitFor();
    assert.equal(await limitedManager.getByLabel('翻訳の並列数', { exact: true }).inputValue(), '1');
    await limitedManager.close();

    Object.assign(harness.control.setup, { supported_translation_previews: true, supported_batch_sizes: Array.from({ length: 32 }, (_, index) => index + 1) });
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('翻訳の並列数', { exact: true }).waitFor();
    assert.equal(await manager.getByLabel('翻訳の並列数', { exact: true }).inputValue(), '32');
    await page.waitForFunction(() => window.__gmValues['cp-translator-translation-previews'] === true && window.__gmValues['cp-translator-supported-batch-sizes'].includes(32));
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await waitForMock(() => submitted().slice(sequential.length).flatMap(request => request.body.segments).length >= 2);
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const parallel = submitted().slice(sequential.length);
    assert.equal(parallel.flatMap(request => request.body.segments).length, 2);
    assert.ok(parallel.every(request => request.body.batch_size === 32));
    assert.equal(parallel.flatMap(request => request.body.segments.map(segment => segment.text)).join(''), source);
    assert.equal(harness.control.values['cp-translator-batch-size'], 32);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), source);
    assert.ok(harness.control.requests.every(request => !/prepare|download/.test(request.path)));
  } finally { await harness.close(); }
});

test('a missing or invalid common count uses sixteen while a valid saved count remains authoritative', async () => {
  for (const initial of [
    {},
    ...[7, '4', 24].map(value => ({ 'cp-translator-batch-size': value })),
  ]) {
    const expected = initial['cp-translator-batch-size'] === 24 ? 24 : 16;
    const harness = await fixture(initial);
    try {
      const page = await harness.page();
      await page.getByLabel('モデルサイズ').waitFor();
      await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
      const body = harness.control.requests.find(request => request.body).body;
      assert.equal(body.batch_size, expected);
      assert.equal(harness.control.values['cp-translator-batch-size'], expected);
      assert.equal(Object.hasOwn(harness.control.values, 'cp-translator-batch-sizes'), false);
    } finally { await harness.close(); }
  }
});

test('the common batch choice mirrors only between trusted native pages and GM preferences', async () => {
  const harness = await fixture({ 'cp-translator-batch-size': 5 });
  try {
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    const manager = await harness.page(harness.api + '/');
    const select = manager.getByLabel('翻訳の並列数', { exact: true });
    await select.waitFor();
    assert.deepEqual(await select.locator('option').allTextContents(), ['1', '2', '3', '4', '5', '6', '8', '10', '12', '16', '20', '24', '32']);
    assert.equal(await manager.evaluate(() => localStorage.getItem('cp-translator-batch-size')), '5');
    const text = await harness.page(harness.api + '/text');
    await text.waitForFunction(() => localStorage.getItem('cp-translator-batch-size') === '5');
    await text.evaluate(() => { localStorage.setItem('cp-translator-batch-size', '20'); window.dispatchEvent(new Event('cp-translator-batch-size-changed')); });
    await manager.waitForFunction(() => window.__gmValues['cp-translator-batch-size'] === 20);
    assert.equal(await select.inputValue(), '20');
    await select.selectOption('24');
    await text.waitForFunction(() => localStorage.getItem('cp-translator-batch-size') === '24' && window.__gmValues['cp-translator-batch-size'] === 24);
    await judge.evaluate(() => { localStorage.setItem('cp-translator-batch-size', '2'); window.dispatchEvent(new Event('cp-translator-batch-size-changed')); });
    await judge.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await judge.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(harness.control.requests.find(request => request.body).body.batch_size, 24);
    assert.equal(harness.control.values['cp-translator-batch-size'], 24);
  } finally { await harness.close(); }
});

test('drag position persists and disabled sites do not bootstrap any API requests', async () => {
  const harness = await fixture();
  try {
    const page = await harness.page();
    const grip = page.getByRole('button', { name: '翻訳バーを移動', exact: true });
    await grip.waitFor();
    const bounds = await grip.boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down(); await page.mouse.move(150, 150, { steps: 8 }); await page.mouse.up();
    await page.waitForFunction(() => window.__gmValues['cp-translator-panel-offset'] != null);
    assert.ok(Number.isFinite(harness.control.values['cp-translator-panel-offset'].x));
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    const rect = await judge.locator('#cp-translator').boundingBox();
    assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x < 300 && rect.y < 300);
    harness.control.values['cp-translator-disabled-sites'] = ['codeforces'];
    const before = harness.control.requests.length;
    const disabled = await harness.page();
    assert.equal(await disabled.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('custom-site bootstrap respects path and enabled state, and translates matched pages as general documents', async () => {
  const harness = await fixture({ 'cp-translator-custom-sites': [{ id: 'custom', pattern: 'https://example.org/articles/*', enabled: true }] });
  try {
    const page = await harness.page('https://example.org/articles/one', article);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).waitFor();
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
    let before = harness.control.requests.length;
    const outside = await harness.page('https://example.org/other/one', article);
    assert.equal(await outside.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, before);
    harness.control.values['cp-translator-custom-sites'][0].enabled = false;
    before = harness.control.requests.length;
    const disabled = await harness.page('https://example.org/articles/two', article);
    assert.equal(await disabled.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, before);
    await disabled.evaluate(() => window.__gmMenus['このページを翻訳']());
    await disabled.waitForFunction(() => document.querySelector('#cp-translator')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.match(await disabled.locator('#p').textContent(), /日本語/);
  } finally { await harness.close(); }
});

test('arXiv HTML defaults to a manual general-document toolbar, excludes abs/pdf and honors its site controls', async () => {
  const harness = await fixture();
  const html = await readFile(new URL('./fixtures/arxiv-html-math-paragraph.html', import.meta.url), 'utf8');
  try {
    const page = await harness.page('https://arxiv.org/html/2402.03300v3', html);
    await page.getByLabel('モデルサイズ').waitFor();
    assert.equal(harness.control.values['cp-translator-auto-sites']?.arxiv, undefined, 'a new general-document site starts with automatic translation off');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    for (const path of ['/abs/2402.03300', '/pdf/2402.03300']) {
      const before = harness.control.requests.length;
      const other = await harness.page('https://arxiv.org' + path, html);
      await other.waitForTimeout(120);
      assert.equal(await other.locator('#cp-translator').count(), 0); assert.equal(harness.control.requests.length, before);
    }
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('arXiv の表示', { exact: true }).waitFor();
    assert.equal(await manager.getByLabel('arXiv の表示', { exact: true }).isChecked(), true);
    assert.equal(await manager.getByLabel('arXiv の自動翻訳', { exact: true }).isChecked(), false);
    assert.equal(await manager.getByRole('link', { name: 'arXiv', exact: true }).getAttribute('href'), 'https://arxiv.org/');
    await manager.getByLabel('arXiv の自動翻訳', { exact: true }).check();
    await page.waitForFunction(() => document.querySelector('#paper-prose').textContent.startsWith('日本語'));
    const sent = harness.control.requests.filter(request => request.body);
    assert.ok(sent.length > 0 && sent.every(request => request.body.document_type === 'general' && Array.isArray(request.body.glossary) && request.body.glossary.length === 0));
    await manager.getByLabel('arXiv の表示', { exact: true }).uncheck();
    const before = harness.control.requests.length, disabled = await harness.page('https://arxiv.org/html/2402.03300v4', html);
    await disabled.waitForTimeout(120);
    assert.equal(await disabled.locator('#cp-translator').count(), 0); assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('an existing custom arXiv rule retains its own automatic choice when general-document support is added', async () => {
  const harness = await fixture({ 'cp-translator-custom-sites': [{ id: 'paper-rule', pattern: 'https://arxiv.org/html/*', enabled: true }], 'cp-translator-auto-sites': { arxiv: false, 'paper-rule': true } });
  try {
    const page = await harness.page('https://arxiv.org/html/1234.56789', '<article class="ltx_document"><p id="p">An example paper paragraph.</p></article>');
    await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
    assert.equal(harness.control.values['cp-translator-auto-sites'].arxiv, false);
    assert.equal(harness.control.values['cp-translator-auto-sites']['paper-rule'], true);
  } finally { await harness.close(); }
});

test('per-site automatic controls isolate judge problems from another judge and a custom blog rule', async () => {
  const harness = await fixture({ 'cp-translator-auto-sites': { codeforces: true, codechef: false, blog: false }, 'cp-translator-custom-sites': [{ id: 'blog', pattern: 'https://codeforces.com/blog/*', enabled: true }] });
  try {
    const judge = await harness.page();
    await judge.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    const chef = await harness.page('https://www.codechef.com/problems/TEST', '<main><div id="problem-statement"><p id="p">Chef problem paragraph.</p></div></main>');
    await chef.getByLabel('モデルサイズ').waitFor();
    const blog = await harness.page('https://codeforces.com/blog/one', article);
    await blog.getByLabel('モデルサイズ').waitFor();
    await blog.waitForTimeout(180);
    assert.equal(await chef.locator('#p').textContent(), 'Chef problem paragraph.');
    assert.ok(!(await blog.locator('#p').textContent()).startsWith('日本語'));
    assert.equal(harness.control.requests.filter(request => request.body).length, 1);
    const manager = await harness.page(harness.api + '/');
    await manager.getByRole('heading', { name: 'このブラウザーの翻訳設定' }).waitFor();
    await manager.getByLabel('https://codeforces.com/blog/* の自動翻訳', { exact: true }).check();
    await blog.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    assert.equal(harness.control.requests.findLast(request => request.body).body.document_type, 'general');
    await manager.getByLabel('Codeforces の表示', { exact: true }).uncheck();
    assert.equal(await manager.getByLabel('Codeforces の自動翻訳', { exact: true }).isDisabled(), true);
    assert.equal(await manager.getByLabel('Codeforces の自動翻訳', { exact: true }).isChecked(), true);
    const before = harness.control.requests.length;
    const disabled = await harness.page('https://codeforces.com/problemset/problem/disabled/A', problem);
    await disabled.waitForTimeout(160);
    assert.equal(await disabled.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('automatic custom sites respect independent path switches and never repeat on their translated DOM', async () => {
  const harness = await fixture({ 'cp-translator-auto-sites': { enabled: true, manual: false, disabled: true }, 'cp-translator-custom-sites': [
    { id: 'enabled', pattern: 'https://example.org/auto/*', enabled: true },
    { id: 'manual', pattern: 'https://example.org/manual/*', enabled: true },
    { id: 'disabled', pattern: 'https://example.org/disabled/*', enabled: false },
  ] });
  try {
    const automatic = await harness.page('https://example.org/auto/one', article);
    await automatic.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    await automatic.getByRole('button', { name: '原文を表示', exact: true }).click();
    await automatic.evaluate(() => document.body.append(document.createElement('aside')));
    const manual = await harness.page('https://example.org/manual/one', article);
    await manual.getByLabel('モデルサイズ').waitFor(); await manual.waitForTimeout(180);
    assert.ok(!(await manual.locator('#p').textContent()).startsWith('日本語'));
    assert.equal(harness.control.requests.filter(request => request.body).length, 1);
    const before = harness.control.requests.length;
    const outside = await harness.page('https://example.org/outside/one', article), disabled = await harness.page('https://example.org/disabled/one', article);
    await disabled.waitForTimeout(160);
    assert.equal(harness.control.requests.length, before);
    assert.equal(await outside.locator('#cp-translator').count(), 0); assert.equal(await disabled.locator('#cp-translator').count(), 0);
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
  } finally { await harness.close(); }
});

test('old plamo settings are neither read nor migrated into the new translator namespace', async () => {
  const old = {
    'plamo-model': '8b', 'plamo-pending-model': '0.8b', 'plamo-batch-size': 3,
    'plamo-batch-sizes': { '0.8b': 1, '2b': 4, '8b': 8 },
    'plamo-auto-translate': true, 'plamo-auto-sites': { codeforces: true },
    'plamo-disabled-sites': ['codeforces'],
    'plamo-custom-sites': [{ id: 'old-site', pattern: 'https://example.org/*', enabled: true }],
    'plamo-glossary': [{ source: 'jury', target: '旧設定' }],
    'plamo-panel-position': 'left-top', 'plamo-panel-offset': { x: .2, y: .3 },
  };
  const harness = await fixture(old);
  try {
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    assert.equal(await judge.getByLabel('モデルサイズ').inputValue(), '2b');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('用語集', { exact: true }).waitFor();
    assert.equal(await manager.getByLabel('翻訳の並列数', { exact: true }).inputValue(), '16');
    assert.equal(await manager.getByLabel('Codeforces の表示', { exact: true }).isChecked(), true);
    assert.equal(await manager.getByLabel('Codeforces の自動翻訳', { exact: true }).isChecked(), false);
    assert.equal(await manager.locator('.custom-site').count(), 0);
    assert.deepEqual(harness.control.values['cp-translator-glossary'], defaultGlossary);
    await manager.getByRole('button', { name: '設定をデフォルトに戻す', exact: true }).click();
    for (const [key, value] of Object.entries(old)) assert.deepEqual(harness.control.values[key], value, key);
    for (const page of [judge, manager]) assert.equal(await page.evaluate(() => window.__gmReadKeys.some(key => key.startsWith('plamo-'))), false);
    assert.equal(Object.hasOwn(harness.control.values, 'cp-translator-auto-sites-migrated'), false);
    assert.equal(Object.hasOwn(harness.control.values, 'cp-translator-glossary-version'), false);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('generated userscript hides automatic MathRound toolbars while explicit manual translation and ordinary Repovive remain available', async () => {
  const harness = await fixture({ 'cp-translator-auto-sites': { repovive: true, override: true }, 'cp-translator-custom-sites': [{ id: 'override', pattern: 'https://repovive.com/*', enabled: true }] });
  try {
    const blocked = await harness.page('https://repovive.com/contests/m1/problems/A', '<title>Gifts - Repovive Math Contest Round 1 | Repovive</title><div class="prose"><p>Restricted problem.</p></div>');
    assert.equal(await blocked.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, 0);
    const numeric = await harness.page('https://repovive.com/contests/1015/problems/A', '<title>Loading | Repovive</title><div class="prose"><p>Restricted problem.</p></div>');
    assert.equal(await numeric.locator('#cp-translator').count(), 0);
    assert.equal(harness.control.requests.length, 0);
    await blocked.screenshot({ path: '.local/repovive-mathround-blocked.png', fullPage: true });
    await blocked.evaluate(() => window.__gmMenus['このページを翻訳']());
    assert.match(await blocked.locator('.prose').textContent(), /^日本語/);
    assert.equal(await blocked.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
    const ordinary = await harness.page('https://repovive.com/contests/30/problems/G', '<title>Math Contest Tricks - Repovive Premier Round 10 | Repovive</title><div class="prose"><p id="p">Ordinary problem paragraph.</p></div>');
    await ordinary.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    assert.equal(harness.control.requests.filter(request => request.body).length, 2);
    await numeric.evaluate(() => { history.pushState({}, '', '/contests/30/problems/G'); document.title = 'Example - Repovive Premier Round 10 | Repovive'; document.querySelector('.prose').innerHTML = '<p id="p">Ordinary problem after navigation.</p>'; });
    await numeric.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    assert.equal(await numeric.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
  } finally { await harness.close(); }
});

for (const item of [
  { name: 'UOJ', url: 'https://uoj.ac/problem/1', language: 'Chinese', html: '<div id="tab-statement"><article class="uoj-article"><p id="p">给定一个整数 <var>n</var>，请求出最大值。</p><pre>1  2\n3</pre></article></div>' },
  { name: 'TLX', url: 'https://tlx.toki.id/courses/basic/chapters/implementation/problems/D', language: 'auto', html: '<div class="programming-problem-statement__text"><div class="html-text"><p id="p">Diberikan sebuah bilangan bulat <var>n</var>.</p><pre>1  2\n3</pre></div></div>' },
]) {
  test(`generated userscript sends ${item.name}'s declared source language through the GM transport`, async () => {
    const harness = await fixture();
    try {
      const page = await harness.page(item.url, item.html);
      await page.getByRole('button', { name: '日本語に翻訳', exact: true }).waitFor();
      await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
      assert.equal(harness.control.requests.find(request => request.body).body.source_lang, item.language);
      assert.equal(await page.locator('pre').textContent(), '1  2\n3');
    } finally { await harness.close(); }
  });
}

async function assertMasterCheckbox(input, checked, indeterminate) {
  assert.deepEqual(await input.evaluate(node => ({ checked: node.checked, indeterminate: node.indeterminate })), { checked, indeterminate });
}

test('site visibility can be switched off and on with one tri-state checkbox while preserving automatic choices and custom rules', async () => {
  const automatic = { codeforces: true, codechef: false, custom: true, other: false };
  const custom = [
    { id: 'custom', pattern: 'https://example.org/articles/*', enabled: true },
    { id: 'other', pattern: 'https://other.example.net/*', enabled: false },
  ];
  const harness = await fixture({
    'cp-translator-disabled-sites': ['codechef', 'future-site'],
    'cp-translator-auto-sites': automatic, 'cp-translator-custom-sites': custom,
  });
  try {
    let page = await harness.page(harness.api + '/');
    let actions = page.getByRole('group', { name: 'サイトの表示を一括変更' });
    let all = actions.getByRole('checkbox', { name: 'すべてのサイトの表示', exact: true });
    assert.equal(await actions.evaluate(node => node.parentElement.tagName === 'TH' && node.parentElement.cellIndex === 1), true);
    assert.equal(await actions.getByRole('checkbox').count(), 1);
    assert.equal(await actions.getByRole('button').count(), 0);
    await assertMasterCheckbox(all, false, true);
    await all.click();
    await assertMasterCheckbox(all, true, false);
    await page.getByLabel('Codeforces の表示', { exact: true }).uncheck();
    await assertMasterCheckbox(all, false, true);
    await all.click();
    await assertMasterCheckbox(all, true, false);
    await page.getByLabel('https://other.example.net/* の表示', { exact: true }).uncheck();
    await assertMasterCheckbox(all, false, true);
    await page.getByLabel('https://other.example.net/* の表示', { exact: true }).check();
    await assertMasterCheckbox(all, true, false);
    await all.click();
    await assertMasterCheckbox(all, false, false);
    let rows = page.locator('#sites > tr, #custom-sites > tr');
    assert.ok(await rows.locator('input[aria-label$=" の表示"]').evaluateAll(inputs => inputs.every(input => !input.checked)));
    assert.ok(await rows.locator('input[aria-label$=" の自動翻訳"]').evaluateAll(inputs => inputs.every(input => input.disabled)));
    await page.close();
    page = await harness.page(harness.api + '/');
    actions = page.getByRole('group', { name: 'サイトの表示を一括変更' });
    all = actions.getByRole('checkbox', { name: 'すべてのサイトの表示', exact: true });
    rows = page.locator('#sites > tr, #custom-sites > tr');
    await actions.waitFor();
    await assertMasterCheckbox(all, false, false);
    assert.ok(await rows.locator('input[aria-label$=" の表示"]').evaluateAll(inputs => inputs.every(input => !input.checked)));
    assert.deepEqual(harness.control.values['cp-translator-auto-sites'], automatic);
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], custom.map(site => ({ ...site, enabled: false })));

    await all.click();
    await assertMasterCheckbox(all, true, false);
    await page.close();
    page = await harness.page(harness.api + '/');
    actions = page.getByRole('group', { name: 'サイトの表示を一括変更' });
    all = actions.getByRole('checkbox', { name: 'すべてのサイトの表示', exact: true });
    rows = page.locator('#sites > tr, #custom-sites > tr');
    await actions.waitFor();
    await assertMasterCheckbox(all, true, false);
    assert.ok(await rows.locator('input[aria-label$=" の表示"]').evaluateAll(inputs => inputs.every(input => input.checked)));
    assert.ok(await rows.locator('input[aria-label$=" の自動翻訳"]').evaluateAll(inputs => inputs.every(input => !input.disabled)));
    assert.equal(await page.getByLabel('Codeforces の自動翻訳', { exact: true }).isChecked(), true);
    assert.equal(await page.getByLabel('CodeChef の自動翻訳', { exact: true }).isChecked(), false);
    assert.equal(await page.getByLabel('https://example.org/articles/* の自動翻訳', { exact: true }).isChecked(), true);
    assert.deepEqual(harness.control.values['cp-translator-auto-sites'], automatic);
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], custom.map(site => ({ ...site, enabled: true })));
    assert.deepEqual(harness.control.values['cp-translator-disabled-sites'], ['future-site']);
    const otherManager = await harness.page(harness.api + '/');
    await otherManager.getByLabel('TLX の表示', { exact: true }).uncheck();
    await page.waitForFunction(() => document.querySelector('[data-cp-translator-ui]').shadowRoot.querySelector('[aria-label="すべてのサイトの表示"]').indeterminate);
    await assertMasterCheckbox(all, false, true);
    assert.deepEqual(harness.control.values['cp-translator-auto-sites'], automatic);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('the automatic column has one tri-state checkbox for all registered sites without changing display choices or unknown preferences', async () => {
  const disabled = ['codechef', 'kattis', 'future-site'];
  const custom = [
    { id: 'custom', pattern: 'https://example.org/articles/*', enabled: true },
    { id: 'other', pattern: 'https://other.example.net/*', enabled: false },
  ];
  const ids = ['codeforces', 'codechef', 'cses', 'kattis', 'repovive', 'uoj', 'tlx', 'project-euler', 'arxiv', 'custom', 'other'];
  const harness = await fixture({
    'cp-translator-disabled-sites': disabled, 'cp-translator-custom-sites': custom,
    'cp-translator-auto-sites': { codeforces: false, codechef: true, custom: false, other: true, 'future-site': true },
  });
  try {
    let page = await harness.page(harness.api + '/');
    let automatic = page.getByRole('group', { name: 'サイトの自動翻訳を一括変更', exact: true });
    let all = automatic.getByRole('checkbox', { name: 'すべてのサイトの自動翻訳', exact: true });
    await automatic.waitFor();
    assert.equal(await automatic.evaluate(node => node.parentElement.tagName === 'TH' && node.parentElement.cellIndex === 2), true);
    assert.equal(await automatic.getByRole('checkbox').count(), 1);
    assert.equal(await automatic.getByRole('button').count(), 0);
    await assertMasterCheckbox(all, false, true);
    const before = await page.locator('#sites > tr input[aria-label$=" の表示"], #custom-sites > tr input[aria-label$=" の表示"]').evaluateAll(inputs => inputs.map(input => input.checked));
    await all.click();
    await assertMasterCheckbox(all, true, false);
    assert.deepEqual(harness.control.values['cp-translator-auto-sites'], { 'future-site': true, ...Object.fromEntries(ids.map(id => [id, true])) });
    assert.equal(await page.getByLabel('CodeChef の自動翻訳', { exact: true }).isChecked(), true);
    assert.equal(await page.getByLabel('CodeChef の自動翻訳', { exact: true }).isDisabled(), true);
    assert.equal(await page.getByLabel('https://other.example.net/* の自動翻訳', { exact: true }).isChecked(), true);
    assert.equal(await page.getByLabel('https://other.example.net/* の自動翻訳', { exact: true }).isDisabled(), true);
    assert.deepEqual(harness.control.values['cp-translator-disabled-sites'], disabled);
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], custom);
    await page.getByLabel('Codeforces の自動翻訳', { exact: true }).uncheck();
    await assertMasterCheckbox(all, false, true);
    await page.getByLabel('Codeforces の自動翻訳', { exact: true }).check();
    await assertMasterCheckbox(all, true, false);
    await page.getByLabel('サイトを追加', { exact: true }).fill('new.example.org');
    await page.getByRole('button', { name: '追加', exact: true }).click();
    await assertMasterCheckbox(all, false, true);
    assert.equal(await page.getByLabel('https://new.example.org/* の自動翻訳', { exact: true }).isChecked(), false);
    await page.getByRole('button', { name: 'https://new.example.org/* を削除', exact: true }).click();
    await assertMasterCheckbox(all, true, false);
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], custom);
    const otherManager = await harness.page(harness.api + '/');
    await otherManager.getByLabel('TLX の自動翻訳', { exact: true }).uncheck();
    await page.waitForFunction(() => document.querySelector('[data-cp-translator-ui]').shadowRoot.querySelector('[aria-label="すべてのサイトの自動翻訳"]').indeterminate);
    await assertMasterCheckbox(all, false, true);
    await otherManager.close();
    await page.close();
    page = await harness.page(harness.api + '/');
    automatic = page.getByRole('group', { name: 'サイトの自動翻訳を一括変更', exact: true });
    all = automatic.getByRole('checkbox', { name: 'すべてのサイトの自動翻訳', exact: true });
    await automatic.waitFor();
    await assertMasterCheckbox(all, false, true);
    await all.click();
    await assertMasterCheckbox(all, true, false);
    await all.click();
    await assertMasterCheckbox(all, false, false);
    assert.deepEqual(harness.control.values['cp-translator-auto-sites'], { 'future-site': true, ...Object.fromEntries(ids.map(id => [id, false])) });
    assert.ok(await page.locator('input[aria-label$=" の自動翻訳"]').evaluateAll(inputs => inputs.every(input => !input.checked)));
    assert.deepEqual(await page.locator('#sites > tr input[aria-label$=" の表示"], #custom-sites > tr input[aria-label$=" の表示"]').evaluateAll(inputs => inputs.map(input => input.checked)), before);
    assert.deepEqual(harness.control.values['cp-translator-disabled-sites'], disabled);
    assert.deepEqual(harness.control.values['cp-translator-custom-sites'], custom);
    const refreshed = await harness.page(harness.api + '/');
    await refreshed.getByRole('checkbox', { name: 'すべてのサイトの自動翻訳', exact: true }).waitFor();
    await assertMasterCheckbox(refreshed.getByRole('checkbox', { name: 'すべてのサイトの自動翻訳', exact: true }), false, false);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

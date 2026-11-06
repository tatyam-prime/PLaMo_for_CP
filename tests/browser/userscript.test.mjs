import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { Script } from 'node:vm';
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
];

async function chooseModel(page, model) {
  const select = page.getByLabel('モデルサイズ');
  await select.focus(); await select.press(model[0]); await select.press('Tab');
  assert.equal(await select.inputValue(), model);
}

async function fixture(initialValues = {}) {
  const context = await browser.newContext();
  const directory = await mkdtemp(join(tmpdir(), 'plamo-userscript-'));
  const control = { values: { ...initialValues }, requests: [], opened: [], delay: 0, status: 200, jobs: new Map(), jobDelays: {}, jobErrors: {}, jobPreviews: {}, creationDelay: 0, setup: { application: 'plamo-for-cp', selected_model: '2b', default_glossary: structuredClone(defaultGlossary), supported_batch_sizes: Array.from({ length: 16 }, (_, index) => index + 1), device: { system: 'Darwin', memory_gb: 24, supported: true }, models: ['0.8b', '2b', '8b'].map((id, index) => ({ id, weight_gb: [.98, 2.41, 6.07][index], compatible: true, available: true })), preparation: { stage: 'idle', model: null } } };
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
      window.__gmValues = values; window.__gmMenus = {};
      window.GM_getValue = (key, fallback) => Object.hasOwn(values, key) ? values[key] : fallback;
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

test('generated Tampermonkey artifact embeds canonical core exactly and limits GM privileges', async () => {
  const harness = await fixture();
  try {
    const canonical = await readFile(new URL('../../browser/translator.js', import.meta.url), 'utf8');
    const pipeline = await readFile(new URL('../../plamo_cp/assets/translation-jobs.js', import.meta.url), 'utf8');
    assert.ok(harness.source.includes(canonical.replace("const API = 'http://127.0.0.1:8765';", `const API = '${harness.api}';`)));
    assert.ok(harness.source.includes(pipeline), 'HTML and the native text workspace must use the exact same pipeline');
    assert.doesNotThrow(() => new Script(harness.source));
    assert.match(harness.source, /@grant\s+GM_xmlhttpRequest/);
    assert.match(harness.source, /@connect\s+127\.0\.0\.1/);
    assert.doesNotMatch(harness.source, /@grant\s+(?:unsafeWindow|GM_cookie)|@connect\s+\*/);
  } finally { await harness.close(); }
});

test('page-authored toolbar events cannot invoke GM actions or alter cross-site preferences', async () => {
  const harness = await fixture({ 'plamo-model': '2b' });
  try {
    const page = await harness.page();
    await page.getByLabel('モデルサイズ').waitFor();
    await page.evaluate(() => {
      const shadow = document.querySelector('#plamo-for-cp').shadowRoot;
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
    assert.equal(harness.control.values['plamo-model'], '2b');
    assert.equal(harness.control.values['plamo-panel-offset'], undefined);
    assert.equal(harness.control.values['plamo-panel-position'], undefined);
    assert.equal(await page.getByLabel('モデルサイズ').inputValue(), '2b');
    await chooseModel(page, '8b');
    await page.getByRole('button', { name: '翻訳バーを移動', exact: true }).press('ArrowLeft');
    await page.getByRole('button', { name: '設定・管理', exact: true }).click();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(harness.control.values['plamo-model'], '8b');
    assert.equal(typeof harness.control.values['plamo-panel-offset'].x, 'number');
    assert.deepEqual(harness.control.opened, [harness.api + '/#browser-settings']);
    assert.equal(harness.control.requests.find(request => request.body)?.body.model, '8b');
  } finally { await harness.close(); }
});

test('generated GM jobs display early completions, refill before the first job ends, and keep original-view buffering safe', async () => {
  const harness = await fixture({ 'plamo-model': '2b', 'plamo-batch-sizes': { '2b': 4 } });
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
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('5 箇所を翻訳しました。1 箇所は失敗'));
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

test('token previews stay inline, trim partial slots, preserve formatting and buffer safely in original view', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true; harness.control.delay = 1100;
  harness.control.setup.supported_translation_previews = true;
  harness.control.jobPreviews[0] = [
    { at: 10, revision: 1, text: '<plm-a>途中<img src=x onerror=alert(1)>[[PLM_' },
    { at: 450, revision: 2, text: '<plm-a>改訂された途中</plm-a> [[PLM_0]]' },
  ];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/73/A', '<div class="problem-statement"><p id="p">Find <strong>the answer</strong> for <var id="v">n</var>.</p></div>');
    const original = await page.locator('#p').textContent(), math = await page.locator('#v').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#p [data-plamo-preview]').waitFor();
    assert.equal(harness.control.values['plamo-translation-previews'], true);
    assert.match(await page.locator('#p strong').textContent(), /途中/);
    assert.equal(await page.locator('#p img').count(), 0);
    assert.doesNotMatch(await page.locator('#p').textContent(), /PLM_/);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(await page.locator('#p').textContent(), original);
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    assert.match(await page.locator('#p').textContent(), /^日本語/);
    assert.equal(await page.locator('#p [data-plamo-preview]').count(), 0);
    assert.equal(await page.locator('#v').evaluate(node => node.outerHTML), math);
  } finally { await harness.close(); }
});

test('Stop discards unverified token previews while retaining completed paragraphs', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true;
  harness.control.jobDelays = { 0: 10, 1: 1800 };
  harness.control.jobPreviews[1] = [{ at: 10, revision: 1, text: '途中 [[PLM_0]]' }];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/74/A', '<div class="problem-statement"><p id="done">First paragraph.</p><p id="pending">Second <var>n</var> paragraph.</p></div>');
    const original = await page.locator('#pending').evaluate(node => node.outerHTML);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#pending [data-plamo-preview]').waitFor();
    await page.waitForFunction(() => document.querySelector('#done').textContent.startsWith('日本語'));
    await page.getByRole('button', { name: '停止', exact: true }).click();
    assert.equal(await page.locator('#pending').evaluate(node => node.outerHTML), original);
    assert.match(await page.locator('#done').textContent(), /^日本語/);
    await page.waitForTimeout(250);
    assert.equal(await page.locator('[data-plamo-preview]').count(), 0);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#done').textContent(), 'First paragraph.');
  } finally { await harness.close(); }
});

test('Stop during sentence-retry previews restores pending source and keeps a completed retry sentence switchable', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true; harness.control.setup.supported_translation_previews = true;
  harness.control.jobMarkerLimit = 4; harness.control.jobDelays = { 0: 500, 1: 1600 };
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '途中 [[PLM_0]]' }];
  harness.control.jobPreviews[1] = [{ at: 10, revision: 1, text: '別の途中 [[PLM_4]]' }];
  try {
    const html = await readFile(new URL('./fixtures/codeforces-2120f-opening.html', import.meta.url), 'utf8');
    const page = await harness.page('https://codeforces.com/problemset/problem/2120/F', html);
    const original = await page.locator('#target').textContent();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#target').textContent.startsWith('日本語: ') && document.querySelector('#target [data-plamo-preview]'));
    await page.getByRole('button', { name: '停止', exact: true }).click();
    const stopped = await page.locator('#target').textContent();
    assert.match(stopped, /^日本語:/); assert.match(stopped, /Taylor next constructs/);
    assert.equal(await page.locator('[data-plamo-preview]').count(), 0);
    await page.waitForTimeout(250); assert.equal(await page.locator('#target').textContent(), stopped);
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#target').textContent(), original);
    await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
    assert.equal(await page.locator('#target').textContent(), stopped);
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
    await page.locator('#p [data-plamo-preview]').waitFor();
    assert.equal([...harness.control.jobs.values()][0].events.length, 0);
    [...harness.control.jobs.values()][0].completions.get('0')();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('失敗'));
    assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), original);
    assert.equal(await page.locator('[data-plamo-preview]').count(), 0);
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
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.match(await page.locator('#p').textContent(), /^日本語/);
    assert.equal(await page.locator('var').textContent(), 'n');
  } finally { await harness.close(); }
});

test('page edits to an inline preview reject later snapshots without overwriting the edited content', async () => {
  const harness = await fixture(); harness.control.setup.supported_translation_jobs = true; harness.control.delay = 1100;
  harness.control.jobPreviews[0] = [{ at: 10, revision: 1, text: '途中 [[PLM_0]]' }, { at: 450, revision: 2, text: '別の途中 [[PLM_0]]' }];
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/77/A', '<div class="problem-statement"><p id="p">Find <var>n</var> integers.</p></div>');
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.locator('#p [data-plamo-preview]').waitFor();
    await page.locator('#p [data-plamo-preview]').evaluate(node => { node.firstChild.textContent = 'Page-authored replacement.'; });
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('ページの内容が更新'));
    assert.match(await page.locator('#p').textContent(), /Page-authored replacement/);
    assert.equal(await page.locator('[data-plamo-preview]').count(), 0);
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
    assert.equal(await page.locator('#plamo-for-cp').count(), 0);
    await page.evaluate(() => window.__gmMenus['このページを翻訳']());
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
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
    assert.equal(await page.locator('#plamo-for-cp').count(), 0);
  } finally { await harness.close(); }
});

test('known judge bootstrap defaults to2B and never auto-translates or downloads', async () => {
  const harness = await fixture({ 'plamo-model': '31b' });
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
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.equal(harness.control.requests.find(request => request.body)?.body.model, '0.8b');
  } finally { await harness.close(); }
});

test('stop aborts a live GM request and leaves untranslated source intact', async () => {
  const harness = await fixture();
  try {
    harness.control.delay = 2000;
    const page = await harness.page();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('翻訳中'));
    for (let attempt = 0; attempt < 50 && !harness.control.requests.some(request => request.body); attempt++) await new Promise(resolve => setTimeout(resolve, 10));
    await page.getByRole('button', { name: '停止', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('停止'));
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
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('ダウンロード'));
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
    assert.equal(await page.locator('html').getAttribute('data-plamo-userscript'), 'ready');
    assert.equal(await page.locator('#browser-settings details').count(), 0);
    assert.equal(await page.getByLabel('ページを開いたら自動で翻訳').count(), 0);
    assert.equal(await page.getByLabel('周辺の原文を参考にする').count(), 0);
    assert.equal(await page.getByLabel('用語集', { exact: true }).inputValue(), defaultGlossary.map(pair => pair.source + ' = ' + pair.target).join('\n'));
    assert.equal(await page.getByLabel('用語集', { exact: true }).getAttribute('rows'), '11');
    for (const [model, size] of [['0.8B', '4'], ['2B', '4'], ['8B', '8']]) assert.equal(await page.getByLabel(`${model} の段落数`, { exact: true }).inputValue(), size);
    const batchSelector = page.getByLabel('2B の段落数', { exact: true });
    assert.deepEqual(await batchSelector.locator('option').evaluateAll(options => options.map(option => option.value)), Array.from({ length: 16 }, (_, index) => String(index + 1)));
    await batchSelector.selectOption('4');
    for (const invalid of ['0', '17', '1.5']) {
      await batchSelector.evaluate((select, value) => { select.add(new Option(value, value)); select.value = value; select.dispatchEvent(new Event('change')); }, invalid);
      assert.equal(await batchSelector.inputValue(), '4');
      assert.equal(await page.evaluate(() => window.__gmValues['plamo-batch-sizes']['2b']), 4);
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
    assert.equal(harness.control.values['plamo-custom-sites'][0].pattern, 'https://example.org/*');
    assert.equal(await page.getByRole('link', { name: 'https://example.org/*', exact: true }).getAttribute('href'), 'https://example.org/');
    const site = page.locator('.custom-site');
    await site.getByRole('button', { name: /を編集/ }).click();
    await page.getByLabel('サイトを追加', { exact: true }).fill('https://example.org/articles/*');
    await page.getByRole('button', { name: '変更を保存', exact: true }).click();
    await page.getByLabel('用語集', { exact: true }).fill('array = 配列\nedge = 辺');
    await page.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await page.getByText('用語集を保存しました（2件）。', { exact: true }).waitFor();
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('plamo-model-selected', { detail: { model: '2b' } })));
    await judge.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('select').value === '2b');
    assert.deepEqual(harness.control.values['plamo-glossary'], [{ source: 'array', target: '配列' }, { source: 'edge', target: '辺' }]);
    assert.equal(harness.control.values['plamo-model'], '2b');
    assert.deepEqual(harness.control.values['plamo-batch-sizes'], { '2b': 4 });
    await site.getByRole('button', { name: /を削除/ }).click();
    await page.getByText('サイトを削除しました。', { exact: true }).waitFor();
    assert.deepEqual(harness.control.values['plamo-custom-sites'], []);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('the management reset button restores saved GM preferences across tabs without clearing capabilities or contacting model APIs', async () => {
  const harness = await fixture({
    'plamo-model': '8b', 'plamo-pending-model': '2b',
    'plamo-disabled-sites': ['codechef'], 'plamo-auto-sites': { kattis: true, custom: true },
    'plamo-custom-sites': [{ id: 'custom', pattern: 'https://example.org/*', enabled: true }],
    'plamo-use-source-context': true, 'plamo-glossary': [{ source: 'array', target: '配列' }],
    'plamo-batch-sizes': { '0.8b': 16, '2b': 16, '8b': 16 },
    'plamo-panel-position': 'left-top', 'plamo-panel-offset': { x: .2, y: .3 },
    'plamo-auto-sites-migrated': true, 'plamo-auto-translate': true,
    'plamo-use-history': false, 'plamo-batch-size': 16,
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
    const derived = await page.evaluate(() => Object.fromEntries(['plamo-supported-batch-sizes', 'plamo-model-weights', 'plamo-translation-jobs', 'plamo-translation-previews', 'plamo-auto-sites-migrated'].map(key => [key, window.__gmValues[key]])));
    await page.getByLabel('用語集', { exact: true }).fill('unsaved = 下書き');
    await reset.click();
    await page.getByText('ブラウザーの設定をデフォルトに戻しました。', { exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => ['plamo-text-prefetch', 'plamo-pdf-prefetch', 'plamo-text-use-context', 'unrelated-origin-setting'].map(key => localStorage.getItem(key))), [null, null, null, 'keep']);
    await judge.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('select').value === '2b' && document.querySelector('#plamo-for-cp').style.right === '14px');
    const values = await page.evaluate(() => window.__gmValues);
    for (const [key, expected] of Object.entries({
      'plamo-model': '2b', 'plamo-disabled-sites': [], 'plamo-auto-sites': {}, 'plamo-custom-sites': [],
      'plamo-glossary': defaultGlossary,
      'plamo-batch-sizes': { '0.8b': 4, '2b': 4, '8b': 8 },
      'plamo-panel-position': 'right-bottom', 'plamo-panel-offset': null,
    })) assert.deepEqual(values[key], expected, key);
    for (const key of ['plamo-pending-model', 'plamo-auto-translate', 'plamo-use-history', 'plamo-use-source-context', 'plamo-batch-size']) assert.equal(Object.hasOwn(values, key), false, key);
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
    for (const [model, size] of [['0.8B', '4'], ['2B', '4'], ['8B', '8']]) assert.equal(await reopened.getByLabel(`${model} の段落数`, { exact: true }).inputValue(), size);
    assert.equal(await page.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.equal(await judge.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'), 'reset must not prepare/delete models, clear the cache or translate');
  } finally { await harness.close(); }
});

test('legacy glossary entries migrate once into an authoritative list, while clear stays empty and general prose excludes unchanged defaults', async () => {
  const harness = await fixture({ 'plamo-glossary': [{ source: 'JURY', target: '審査員' }, { source: 'tree', target: '木' }] });
  try {
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('用語集', { exact: true }).waitFor();
    const expected = [...defaultGlossary.slice(0, 4), { source: 'JURY', target: '審査員' }, ...defaultGlossary.slice(5), { source: 'tree', target: '木' }];
    assert.equal(await manager.getByLabel('用語集', { exact: true }).inputValue(), expected.map(pair => pair.source + ' = ' + pair.target).join('\n'));
    assert.deepEqual(harness.control.values['plamo-glossary'], expected);
    assert.equal(harness.control.values['plamo-glossary-version'], 1);
    const judge = await harness.page();
    await judge.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await judge.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.deepEqual(harness.control.requests.find(request => request.body)?.body.glossary, expected);
    assert.equal(await judge.evaluate(() => localStorage.getItem('plamo-glossary')), null, 'site origin must not receive the native glossary mirror');
    const general = await harness.page('https://example.org/article', article);
    const before = harness.control.requests.length;
    await general.evaluate(() => window.__gmMenus['このページを翻訳']());
    assert.ok(harness.control.requests.slice(before).filter(request => request.body).every(request => JSON.stringify(request.body.glossary) === JSON.stringify([{ source: 'JURY', target: '審査員' }, { source: 'tree', target: '木' }])));
    await manager.getByLabel('用語集', { exact: true }).fill('');
    await manager.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await manager.getByText('用語集を保存しました（0件）。', { exact: true }).waitFor();
    await judge.waitForFunction(() => window.__gmValues['plamo-glossary']?.length === 0);
    const clearedBefore = harness.control.requests.length;
    await judge.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await judge.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const sent = harness.control.requests.slice(clearedBefore).filter(request => request.body);
    assert.ok(sent.length > 0 && sent.every(request => Array.isArray(request.body.glossary) && request.body.glossary.length === 0));
    const reopened = await harness.page(harness.api + '/');
    await reopened.getByLabel('用語集', { exact: true }).waitFor();
    assert.equal(await reopened.getByLabel('用語集', { exact: true }).inputValue(), '');
    assert.deepEqual(harness.control.values['plamo-glossary'], []);
  } finally { await harness.close(); }
});

test('the trusted text workspace mirrors glossary updates and clears without changing its draft', async () => {
  const harness = await fixture({ 'plamo-glossary': [] });
  try {
    const manager = await harness.page(harness.api + '/');
    await manager.getByLabel('用語集', { exact: true }).waitFor();
    assert.deepEqual(harness.control.values['plamo-glossary'], defaultGlossary, 'an old empty additional-glossary list migrates to the visible defaults once');
    const text = await harness.page(harness.api + '/text');
    await text.waitForFunction(count => JSON.parse(localStorage.getItem('plamo-glossary') || 'null')?.length === count, defaultGlossary.length);
    await text.locator('#draft').fill('Keep this unfinished text.');
    await text.evaluate(() => { window.__glossaryChanges = 0; addEventListener('storage', event => { if (event.key === 'plamo-glossary') window.__glossaryChanges++; }); });
    await manager.getByLabel('用語集', { exact: true }).fill('');
    await manager.getByRole('button', { name: '用語集を保存', exact: true }).click();
    await text.waitForFunction(() => localStorage.getItem('plamo-glossary') === '[]' && window.__glossaryChanges > 0);
    assert.equal(await text.locator('#draft').inputValue(), 'Keep this unfinished text.');
    await manager.getByRole('button', { name: '設定をデフォルトに戻す', exact: true }).click();
    await text.waitForFunction(count => JSON.parse(localStorage.getItem('plamo-glossary'))?.length === count, defaultGlossary.length);
    assert.deepEqual(await text.evaluate(() => JSON.parse(localStorage.getItem('plamo-glossary'))), defaultGlossary);
    assert.equal(await text.locator('#draft').inputValue(), 'Keep this unfinished text.');
    assert.equal(await text.evaluate(() => typeof window.resetBrowserPreferences), 'undefined');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

test('a new default term upgrades only an unchanged version1 default glossary, preserving cleared and edited lists', async () => {
  const oldDefaults = defaultGlossary.slice(0, 10), edited = oldDefaults.map(pair => pair.source === 'jury' ? { ...pair, target: '審査員' } : pair);
  for (const [previous, saved, expected] of [[defaultGlossary.slice(0, 5), defaultGlossary.slice(0, 5), defaultGlossary], [defaultGlossary.slice(0, 6), defaultGlossary.slice(0, 6), defaultGlossary], [oldDefaults, oldDefaults, defaultGlossary], [oldDefaults, [], []], [oldDefaults, edited, edited], [oldDefaults, oldDefaults.slice(1), oldDefaults.slice(1)]]) {
    const harness = await fixture({ 'plamo-glossary-version': 1, 'plamo-default-glossary': previous, 'plamo-glossary': saved });
    try {
      const page = await harness.page(harness.api + '/');
      await page.getByLabel('用語集', { exact: true }).waitFor();
      assert.deepEqual(harness.control.values['plamo-glossary'], expected);
      assert.deepEqual(harness.control.values['plamo-default-glossary'], defaultGlossary);
      assert.equal(await page.getByLabel('用語集', { exact: true }).inputValue(), expected.map(pair => pair.source + ' = ' + pair.target).join('\n'));
      assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    } finally { await harness.close(); }
  }
});

test('generated transport uses per-model batches without removed source context and respects device limits', async () => {
  const harness = await fixture({ 'plamo-model': '2b', 'plamo-batch-sizes': { '0.8b': 1, '2b': 2, '8b': 4 }, 'plamo-use-source-context': true });
  try {
    const page = await harness.page();
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const first = harness.control.requests.find(request => request.body).body;
    assert.equal(first.model, '2b'); assert.equal(first.batch_size, 2); assert.equal(first.segments.length, 2);
    assert.equal(first.source_context, '');
    assert.doesNotMatch(first.source_context, /日本語|\[\[PLM_|<\/?plm-/);
    assert.equal(Object.hasOwn(first, 'history'), false);
    harness.control.setup.supported_batch_sizes = [1];
    const manager = await harness.page(harness.api + '/');
    await manager.getByRole('heading', { name: 'このブラウザーの翻訳設定' }).waitFor();
    assert.equal(await manager.getByLabel('2B の段落数', { exact: true }).inputValue(), '1');
    assert.equal(await manager.getByLabel('2B の段落数', { exact: true }).locator('option[value="4"]').isDisabled(), true);
    assert.equal(await manager.getByLabel('8B の段落数', { exact: true }).locator('option[value="8"]').isDisabled(), true);
    const constrained = await harness.page();
    await constrained.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await constrained.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.ok(harness.control.requests.filter(request => request.body).slice(1).every(request => request.body.batch_size === 1 && request.body.segments.length === 1));
  } finally { await harness.close(); }
});

test('fresh per-model defaults use4/4/8 paragraphs and explicit saved choices remain intact', async () => {
  const harness = await fixture();
  const eightParagraphs = `<div class="problem-statement">${Array.from({ length: 8 }, (_, index) => `<p>Paragraph ${index} in English.</p>`).join('')}</div>`;
  try {
    const page = await harness.page('https://codeforces.com/problemset/problem/42/A', eightParagraphs);
    await page.getByLabel('モデルサイズ').waitFor();
    for (const [model, batch, lengths] of [['0.8b', 4, [4, 4]], ['2b', 4, [4, 4]], ['8b', 8, [8]]]) {
      await chooseModel(page, model);
      const before = harness.control.requests.length;
      await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
      const sent = harness.control.requests.slice(before).filter(request => request.body);
      assert.deepEqual(sent.map(request => request.body.segments.length), lengths);
      assert.ok(sent.every(request => request.body.model === model && request.body.batch_size === batch));
    }
    assert.equal(harness.control.values['plamo-batch-sizes'], undefined, 'defaults must not overwrite user storage');
    const saved = { '0.8b': 4, '2b': 1, '8b': 2 };
    await page.evaluate(value => GM_setValue('plamo-batch-sizes', value), saved);
    await chooseModel(page, '2b');
    const before = harness.control.requests.length;
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    const sent = harness.control.requests.slice(before).filter(request => request.body);
    assert.equal(sent.length, 8);
    assert.ok(sent.every(request => request.body.batch_size === 1 && request.body.segments.length === 1));
    assert.deepEqual(harness.control.values['plamo-batch-sizes'], saved);
    harness.control.setup.supported_batch_sizes = [1];
    const constrained = await harness.page('https://codeforces.com/problemset/problem/43/A', eightParagraphs);
    await chooseModel(constrained, '8b');
    const limitedBefore = harness.control.requests.length;
    await constrained.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await constrained.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.ok(harness.control.requests.slice(limitedBefore).filter(request => request.body).every(request => request.body.batch_size === 1));
    assert.deepEqual(harness.control.values['plamo-batch-sizes'], saved, 'device fallback must preserve explicit settings');
  } finally { await harness.close(); }
});

test('generated GM jobs ramp up a saved sixteen-paragraph window before any completion', async () => {
  const harness = await fixture({ 'plamo-model': '8b', 'plamo-batch-sizes': { '8b': 16 } });
  harness.control.setup.supported_translation_jobs = true;
  harness.control.delay = 2000;
  try {
    const html = '<div class="problem-statement">' + Array.from({ length: 18 }, (_, index) => `<p>Paragraph ${index} in English.</p>`).join('') + '</div>';
    const page = await harness.page('https://codeforces.com/problemset/problem/44/A', html);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    for (let attempt = 0; attempt < 180 && harness.control.requests.filter(request => request.path === '/api/translation-jobs').length < 16; attempt++) await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(harness.control.requests.filter(request => request.path === '/api/translation-jobs').length, 16);
    assert.ok([...harness.control.jobs.values()].every(job => job.events.length === 0));
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('18 箇所を翻訳しました'));
    const posted = harness.control.requests.filter(request => request.path === '/api/translation-jobs');
    assert.deepEqual(posted.map(request => request.body.segments.length), Array(18).fill(1));
    assert.ok(posted.every(request => request.body.batch_size === 16));
    assert.equal(await page.locator('.problem-statement p').evaluateAll(nodes => nodes.every(node => node.textContent.startsWith('日本語: '))), true);
    assert.deepEqual(harness.control.values['plamo-batch-sizes'], { '8b': 16 });
  } finally { await harness.close(); }
});

test('drag position persists and disabled sites do not bootstrap any API requests', async () => {
  const harness = await fixture();
  try {
    const page = await harness.page();
    const grip = page.getByRole('button', { name: '翻訳バーを移動', exact: true });
    await grip.waitFor();
    const bounds = await grip.boundingBox();
    const panelBounds = await page.locator('#plamo-for-cp').locator('.panel').boundingBox();
    assert.ok(Math.abs(bounds.y + bounds.height / 2 - panelBounds.y - panelBounds.height / 2) <= 1, 'grip is vertically centered across controls and attribution');
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down(); await page.mouse.move(150, 150, { steps: 8 }); await page.mouse.up();
    await page.waitForFunction(() => window.__gmValues['plamo-panel-offset'] != null);
    assert.ok(Number.isFinite(harness.control.values['plamo-panel-offset'].x));
    const judge = await harness.page();
    await judge.getByLabel('モデルサイズ').waitFor();
    const rect = await judge.locator('#plamo-for-cp').boundingBox();
    assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x < 300 && rect.y < 300);
    harness.control.values['plamo-disabled-sites'] = ['codeforces'];
    const before = harness.control.requests.length;
    const disabled = await harness.page();
    assert.equal(await disabled.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('custom-site bootstrap respects path and enabled state, and translates matched pages as general documents', async () => {
  const harness = await fixture({ 'plamo-custom-sites': [{ id: 'custom', pattern: 'https://example.org/articles/*', enabled: true }] });
  try {
    const page = await harness.page('https://example.org/articles/one', article);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).waitFor();
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#plamo-for-cp').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
    let before = harness.control.requests.length;
    const outside = await harness.page('https://example.org/other/one', article);
    assert.equal(await outside.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, before);
    harness.control.values['plamo-custom-sites'][0].enabled = false;
    before = harness.control.requests.length;
    const disabled = await harness.page('https://example.org/articles/two', article);
    assert.equal(await disabled.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, before);
    await disabled.evaluate(() => window.__gmMenus['このページを翻訳']());
    await disabled.waitForFunction(() => document.querySelector('#plamo-for-cp')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
    assert.match(await disabled.locator('#p').textContent(), /日本語/);
  } finally { await harness.close(); }
});

test('arXiv HTML defaults to a manual general-document toolbar, excludes abs/pdf and honors its site controls', async () => {
  const harness = await fixture({ 'plamo-auto-translate': true });
  const html = await readFile(new URL('./fixtures/arxiv-html-math-paragraph.html', import.meta.url), 'utf8');
  try {
    const page = await harness.page('https://arxiv.org/html/2402.03300v3', html);
    await page.getByLabel('モデルサイズ').waitFor();
    assert.equal(harness.control.values['plamo-auto-sites']?.arxiv, undefined, 'legacy global auto must not opt a new general-document site in');
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
    for (const path of ['/abs/2402.03300', '/pdf/2402.03300']) {
      const before = harness.control.requests.length;
      const other = await harness.page('https://arxiv.org' + path, html);
      await other.waitForTimeout(120);
      assert.equal(await other.locator('#plamo-for-cp').count(), 0); assert.equal(harness.control.requests.length, before);
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
    assert.equal(await disabled.locator('#plamo-for-cp').count(), 0); assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('an existing custom arXiv rule retains its own automatic choice when general-document support is added', async () => {
  const harness = await fixture({ 'plamo-custom-sites': [{ id: 'paper-rule', pattern: 'https://arxiv.org/html/*', enabled: true }], 'plamo-auto-sites': { arxiv: false, 'paper-rule': true } });
  try {
    const page = await harness.page('https://arxiv.org/html/1234.56789', '<article class="ltx_document"><p id="p">An example paper paragraph.</p></article>');
    await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
    assert.equal(harness.control.values['plamo-auto-sites'].arxiv, false);
    assert.equal(harness.control.values['plamo-auto-sites']['paper-rule'], true);
  } finally { await harness.close(); }
});

test('per-site automatic controls isolate judge problems from another judge and a custom blog rule', async () => {
  const harness = await fixture({ 'plamo-auto-sites': { codeforces: true, codechef: false, blog: false }, 'plamo-custom-sites': [{ id: 'blog', pattern: 'https://codeforces.com/blog/*', enabled: true }] });
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
    assert.equal(await disabled.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, before);
  } finally { await harness.close(); }
});

test('automatic custom sites respect independent path switches and never repeat on their translated DOM', async () => {
  const harness = await fixture({ 'plamo-auto-sites': { enabled: true, manual: false, disabled: true }, 'plamo-custom-sites': [
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
    assert.equal(await outside.locator('#plamo-for-cp').count(), 0); assert.equal(await disabled.locator('#plamo-for-cp').count(), 0);
    assert.ok(harness.control.requests.filter(request => request.body).every(request => request.body.document_type === 'general'));
  } finally { await harness.close(); }
});

test('legacy global automatic mode migrates once for enabled sites and preserves explicit off choices', async () => {
  const harness = await fixture({ 'plamo-auto-translate': true, 'plamo-auto-sites': { codeforces: false, custom: false }, 'plamo-disabled-sites': ['codechef'], 'plamo-custom-sites': [
    { id: 'custom', pattern: 'https://example.org/manual/*', enabled: true }, { id: 'enabled', pattern: 'https://example.org/auto/*', enabled: true }, { id: 'disabled', pattern: 'https://example.org/disabled/*', enabled: false },
  ] });
  try {
    const page = await harness.page('https://unconfigured.example.net/', article);
    await page.waitForFunction(() => GM_getValue('plamo-auto-sites-migrated', false) === true);
    const migrated = harness.control.values['plamo-auto-sites'];
    assert.equal(migrated.codeforces, false); assert.equal(migrated.custom, false); assert.equal(migrated.codechef, undefined); assert.equal(migrated.disabled, undefined);
    assert.equal(migrated.cses, true); assert.equal(migrated.kattis, true); assert.equal(migrated.spoj, undefined); assert.equal(migrated.enabled, true);
    assert.equal(harness.control.values['plamo-auto-translate'], undefined);
    assert.equal(harness.control.requests.length, 0);
    harness.control.values['plamo-auto-translate'] = true;
    harness.control.values['plamo-custom-sites'].push({ id: 'later', pattern: 'https://later.example.org/*', enabled: true });
    const later = await harness.page('https://later.example.org/one', article);
    await later.getByLabel('モデルサイズ').waitFor(); await later.waitForTimeout(160);
    assert.equal(harness.control.values['plamo-auto-sites'].later, undefined);
    assert.ok(harness.control.requests.every(request => request.method === 'GET'));
  } finally { await harness.close(); }
});

for (const item of [{ id: 'spoj', url: 'https://www.spoj.com/problems/TEST/' }, { id: 'dmoj', url: 'https://dmoj.ca/problem/TEST' }]) {
test('retired ' + item.id + ' automatic preferences are cleared and never bootstrap the removed adapter', async () => {
  const harness = await fixture({ 'plamo-auto-sites': { [item.id]: true }, 'plamo-disabled-sites': [item.id] });
  try {
    const page = await harness.page(item.url, '<div id="problem-body"><p>Find the answer.</p></div>');
    assert.equal(await page.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, 0);
    assert.equal(harness.control.values['plamo-auto-sites'][item.id], undefined);
    assert.ok(!harness.control.values['plamo-disabled-sites'].includes(item.id));
  } finally { await harness.close(); }
});
}

test('generated userscript hides automatic MathRound toolbars while explicit manual translation and ordinary Repovive remain available', async () => {
  const harness = await fixture({ 'plamo-auto-sites': { repovive: true, override: true }, 'plamo-custom-sites': [{ id: 'override', pattern: 'https://repovive.com/*', enabled: true }] });
  try {
    const blocked = await harness.page('https://repovive.com/contests/m1/problems/A', '<title>Gifts - Repovive Math Contest Round 1 | Repovive</title><div class="prose"><p>Restricted problem.</p></div>');
    assert.equal(await blocked.locator('#plamo-for-cp').count(), 0);
    assert.equal(harness.control.requests.length, 0);
    const numeric = await harness.page('https://repovive.com/contests/1015/problems/A', '<title>Loading | Repovive</title><div class="prose"><p>Restricted problem.</p></div>');
    assert.equal(await numeric.locator('#plamo-for-cp').count(), 0);
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

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';

let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser?.close(); });

function snapshot() {
  const models = [
    { id: '0.8b', weight_gb: .57, min_memory_gb: 8, compatible: true, available: false },
    { id: '2b', weight_gb: 1.81, min_memory_gb: 12, compatible: true, available: true },
    { id: '8b', weight_gb: 5.28, min_memory_gb: 24, compatible: false, available: false },
  ];
  return {
    setup: { application: 'plamo-for-cp', selected_model: '2b', quiet_level: 0, quiet_mode: false, device: { system: 'Windows', memory_gb: 16, supported: true }, models, preparation: { stage: 'idle', model: null, bytes_received: 0, error: null }, download: { stage: 'idle', model: null, bytes_received: 0, error: null } },
    health: { app: 'plamo-for-cp', version: '1.0.1', engine: { loaded_model: null, state: 'idle' } },
    storage: { folder: 'C:\\Users\\競プロer\\Models', shared: false, models: [...models.map(model => ({ ...model, downloaded: model.available, on_disk_bytes: model.available ? 2 * 1024 ** 3 : 0 })), { id: '31b', on_disk_bytes: 0 }] },
    cache: { entries: 12, bytes: 512 * 1024, max_bytes: 1024 ** 2, retention_days: 7 },
  };
}

async function fixture() {
  const page = await browser.newPage();
  const control = { ...snapshot(), offline: false, actionError: null, requests: [], folderResult: null, folderWait: null, cancelWait: null };
  const html = await readFile(new URL('../../plamo_cp/assets/setup.html', import.meta.url));
  const script = await readFile(new URL('../../plamo_cp/assets/setup.js', import.meta.url));
  const versions = await readFile(new URL('../../plamo_cp/assets/userscript-version.js', import.meta.url));
  await page.route('http://127.0.0.1:44894/', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }));
  await page.route('http://127.0.0.1:44894/setup.js', route => route.fulfill({ contentType: 'application/javascript; charset=utf-8', body: script }));
  await page.route('http://127.0.0.1:44894/userscript-version.js', route => route.fulfill({ contentType: 'application/javascript; charset=utf-8', body: versions }));
  await page.route('http://127.0.0.1:44894/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.method() === 'POST' ? request.postDataJSON() : null;
    control.requests.push({ path, method: request.method(), body, client: request.headers()['x-plamo-client'] });
    if (control.offline) return route.abort('connectionrefused');
    if (request.method() === 'GET') return route.fulfill({ json: ({ '/api/setup': control.setup, '/api/health': control.health, '/api/models/storage': control.storage, '/api/cache': control.cache })[path] });
    if (control.actionError) return route.fulfill({ status: 409, json: { detail: control.actionError } });
    let result = {};
    if (path === '/api/models/download') { control.setup.download = { stage: 'downloading', model: body.model, bytes_received: 1024 ** 2, error: null }; result = { stage: 'queued' }; }
    if (path === '/api/models/download/cancel') { control.setup.download.stage = 'cancelling'; if (control.cancelWait) await control.cancelWait; control.setup.download.stage = 'cancelled'; result = { cancelled: true }; }
    if (path === '/api/setup/prepare') { control.setup.preparation = { stage: 'loading', model: body.model, bytes_received: 0 }; control.setup.selected_model = body.model; control.health.engine.state = 'loading'; result = { stage: 'queued' }; }
    if (path === '/api/models/unload') { control.health.engine.loaded_model = null; control.setup.preparation.stage = 'idle'; result = { unloaded: true }; }
    if (path === '/api/models/delete') { control.storage.models.find(model => model.id === body.model).on_disk_bytes = 0; result = { deleted: true }; }
    if (path === '/api/models/choose-folder') { if (control.folderWait) await control.folderWait; result = control.folderResult || { cancelled: true }; if (!result.cancelled) { control.storage.folder = result.folder; control.health.engine.loaded_model = null; control.setup.preparation.stage = 'idle'; } }
    if (path === '/api/cache/policy') { control.cache.max_bytes = body.max_kib * 1024; result = control.cache; }
    if (path === '/api/cache/clear') { control.cache.entries = 0; control.cache.bytes = 0; result = control.cache; }
    if (path === '/api/performance') {
      control.setup.quiet_level = body.quiet_level ?? (body.quiet_mode ? 3 : 0);
      control.setup.quiet_mode = control.setup.quiet_level > 0;
      result = { quiet_level: control.setup.quiet_level, quiet_mode: control.setup.quiet_mode };
    }
    return route.fulfill({ status: path === '/api/models/download' ? 202 : 200, json: result });
  });
  await page.goto('http://127.0.0.1:44894/');
  await page.getByText('アプリに接続済み', { exact: true }).waitFor();
  return { page, control };
}
async function poll(page) { await page.evaluate(() => refresh()); }
const row = (page, model) => page.locator(`[data-model="${model}"]`);
const posts = control => control.requests.filter(request => request.method === 'POST');

async function mountBrowserSettings(page, version, { ready = true, notify = true, legacy = false } = {}) {
  await page.evaluate(({ version, ready, notify, legacy }) => {
    const host = document.createElement('div'); host.setAttribute(legacy ? 'data-plamo-ui' : 'data-cp-translator-ui', '');
    if (version !== undefined) host.setAttribute(legacy ? 'data-plamo-userscript-version' : 'data-cp-translator-userscript-version', version);
    const heading = document.createElement('h2'); heading.textContent = 'Installed browser settings';
    host.attachShadow({ mode: 'open' }).append(heading);
    document.getElementById('browser-settings').replaceChildren(host);
    delete document.documentElement.dataset.plamoUserscript;
    delete document.documentElement.dataset.cpTranslatorUserscript;
    if (ready) document.documentElement.dataset[legacy ? 'plamoUserscript' : 'cpTranslatorUserscript'] = 'ready';
    if (notify) document.dispatchEvent(new Event(legacy ? 'plamo-browser-settings-ready' : 'cp-translator-browser-settings-ready'));
  }, { version, ready, notify, legacy });
}

test('native update notice compares numeric versions without blocking ongoing work or changing settings', async () => {
  const { page, control } = await fixture();
  try {
    control.health.userscript_version = '0.4.10';
    control.health.engine = { loaded_model: '2b', state: 'translating' };
    await poll(page);
    await mountBrowserSettings(page, '0.4.9');
    const update = page.locator('#userscript-update');
    await update.waitFor({ state: 'visible' });
    assert.match(await page.locator('#userscript-update-detail').textContent(), /現在 v0\.4\.9 → 最新 v0\.4\.10/);
    assert.match(await update.textContent(), /再読み込み/);
    const link = update.getByRole('link', { name: '更新する', exact: true });
    assert.equal(await link.getAttribute('href'), '/install.user.js');
    assert.equal(await link.getAttribute('target'), '_blank');
    assert.match(await link.getAttribute('rel'), /noopener/);
    assert.equal(await update.evaluate(node => node.nextElementSibling?.id), 'browser-settings');
    assert.equal(await page.locator('#browser-settings').getByRole('heading', { name: 'Installed browser settings' }).count(), 1);
    assert.equal(await page.getByLabel('推論の稼働率', { exact: true }).isEnabled(), true);
    assert.deepEqual(control.health.engine, { loaded_model: '2b', state: 'translating' });
    assert.deepEqual(posts(control), []);
    // Attribute changes must refresh the notice without another API poll or a
    // full native page reload after the newly installed script mounts.
    await page.locator('#browser-settings > [data-cp-translator-ui]').evaluate(host => { host.dataset.cpTranslatorUserscriptVersion = '0.4.10'; });
    await update.waitFor({ state: 'hidden' });
  } finally { await page.close(); }
});

test('same/newer installed versions and unfamiliar or missing version data never request an update', async () => {
  const { page, control } = await fixture();
  try {
    for (const item of [
      { installed: '0.4.16', available: '0.4.16' },
      { installed: '0.4.17', available: '0.4.16' },
      { installed: '0.4.10', available: '0.4.9' },
      { installed: '0.4.15', available: undefined },
      { installed: '0.4.15', available: 'not-a-version' },
      { installed: '0.4.15', available: '99999999999999999999.0.0' },
      { installed: '', available: '0.4.16' },
      { installed: 'broken', available: '0.4.16' },
      { installed: '0.4.15', available: '<img src=x onerror="window.__versionXSS=true">' },
    ]) {
      control.health.userscript_version = item.available;
      await poll(page);
      await mountBrowserSettings(page, item.installed);
      assert.equal(await page.locator('#userscript-update').isHidden(), true, JSON.stringify(item));
    }
    assert.equal(await page.locator('#userscript-update img, #userscript-update script').count(), 0);
    assert.equal(await page.evaluate(() => window.__versionXSS), undefined);
    assert.equal(await page.getByLabel('推論の稼働率', { exact: true }).isEnabled(), true);
    assert.deepEqual(posts(control), []);
  } finally { await page.close(); }
});

test('an installed legacy settings host is distinguished from an absent or unrelated userscript UI', async () => {
  const { page, control } = await fixture();
  try {
    control.health.userscript_version = '0.4.16';
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    assert.equal(await page.locator('#browser-settings-placeholder').isVisible(), true);
    await page.evaluate(() => {
      document.documentElement.dataset.plamoUserscript = 'ready';
      const unrelated = document.createElement('div'); unrelated.dataset.plamoUi = ''; document.body.append(unrelated);
      document.dispatchEvent(new Event('plamo-browser-settings-ready'));
    });
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    await mountBrowserSettings(page, undefined, { ready: false, legacy: true });
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    await page.evaluate(() => { document.documentElement.dataset.plamoUserscript = 'ready'; document.dispatchEvent(new Event('plamo-browser-settings-ready')); });
    await page.locator('#userscript-update').waitFor({ state: 'visible' });
    assert.match(await page.locator('#userscript-update-detail').textContent(), /最新版は v0\.4\.16/);
    assert.doesNotMatch(await page.locator('#userscript-update-detail').textContent(), /undefined|null|現在 v/);
    control.health.userscript_version = '0.4.15';
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    delete control.health.userscript_version;
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    assert.deepEqual(posts(control), []);
  } finally { await page.close(); }
});

test('delayed userscript mounting is observed and its update notice follows connection loss and recovery', async () => {
  const { page, control } = await fixture();
  try {
    control.health.userscript_version = '0.4.16';
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    // No ready event: observe the late host insertion itself.
    await mountBrowserSettings(page, '0.4.15', { notify: false });
    await page.locator('#userscript-update').waitFor({ state: 'visible' });
    control.offline = true;
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isHidden(), true);
    control.offline = false;
    await poll(page);
    assert.equal(await page.locator('#userscript-update').isVisible(), true);
    await mountBrowserSettings(page, '0.4.16', { notify: false });
    await page.locator('#userscript-update').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#userscript-update').count(), 1);
    assert.deepEqual(posts(control), []);
  } finally { await page.close(); }
});

test('eight logarithmic utilization levels can change during translation and stay saved when management is reopened', async () => {
  const { page, control } = await fixture();
  try {
    const quiet = page.getByLabel('推論の稼働率', { exact: true });
    assert.equal(await quiet.inputValue(), '0');
    assert.deepEqual(await quiet.locator('option').allTextContents(), ['100%（通常）', '80%', '64%', '50%', '40%', '32%', '25%', '20%']);
    assert.equal(posts(control).length, 0);
    control.health.engine.state = 'translating';
    await poll(page);
    assert.equal(await quiet.isEnabled(), true);
    await quiet.selectOption('3');
    await page.getByText('推論の稼働率を50%にしました', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#quiet-level').disabled);
    await page.reload();
    await page.getByText('アプリに接続済み', { exact: true }).waitFor();
    assert.equal(await quiet.inputValue(), '3');
    assert.equal(control.setup.selected_model, '2b');
    assert.equal(control.health.engine.state, 'translating');
    assert.deepEqual(posts(control), [{ path: '/api/performance', method: 'POST', body: { quiet_level: 3 }, client: 'setup' }]);
    await quiet.selectOption('7');
    await page.getByText('推論の稼働率を20%にしました', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#quiet-level').disabled);
    await quiet.selectOption('0');
    await page.getByText('推論の稼働率を100%にしました', { exact: true }).waitFor();
    assert.deepEqual(posts(control).map(request => request.body), [{ quiet_level: 3 }, { quiet_level: 7 }, { quiet_level: 0 }]);
    control.offline = true;
    await poll(page);
    assert.equal(await quiet.isDisabled(), true);
  } finally { await page.close(); }
});

test('a rejected utilization change restores the saved level and reports the reason', async () => {
  const { page, control } = await fixture();
  try {
    control.actionError = '静音モードを適用できませんでした。';
    await page.getByLabel('推論の稼働率', { exact: true }).selectOption('3');
    await page.getByText(control.actionError, { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('#quiet-level').value === '0' && !document.querySelector('#quiet-level').disabled);
    assert.equal(control.setup.quiet_mode, false);
    assert.equal(control.setup.quiet_level, 0);
    assert.equal(posts(control).length, 1);
  } finally { await page.close(); }
});

test('the former quiet-mode switch is displayed as its original 50 percent level', async () => {
  const { page, control } = await fixture();
  try {
    delete control.setup.quiet_level;
    control.setup.quiet_mode = true;
    await poll(page);
    assert.equal(await page.getByLabel('推論の稼働率', { exact: true }).inputValue(), '3');
    assert.equal(posts(control).length, 0);
  } finally { await page.close(); }
});

test('dashboard shows actual storage, selected versus loaded state, and never starts downloads on render', async () => {
  const { page, control } = await fixture();
  assert.match(await page.title(), /アプリの管理/);
  assert.equal(await page.locator('#app-version').textContent(), 'v1.0.1');
  assert.equal(await page.locator('#app-version').isVisible(), true);
  assert.equal(await page.locator('#folder').textContent(), control.storage.folder);
  assert.equal(await page.locator('#selected-model').textContent(), '2B');
  assert.match(await page.locator('#engine-state').textContent(), /未読み込み/);
  assert.match(await row(page, '2b').textContent(), /2\.00 GiB/);
  assert.equal(await row(page, '2b').locator('.recommended').isVisible(), true);
  assert.equal(await row(page, '0.8b').locator('.recommended').isVisible(), false);
  assert.match(await row(page, '0.8b').locator('.model-description').textContent(), /省メモリ/);
  assert.equal(await row(page, '8b').locator('[data-action="prepare"]').isDisabled(), true);
  assert.equal(await page.locator('[data-model="31b"]').count(), 0);
  assert.equal(await page.locator('details').count(), 0);
  assert.equal(await page.locator('#browser-settings').isVisible(), true);
  assert.equal(await page.getByRole('link', { name: '翻訳スクリプトをインストール' }).getAttribute('href'), '/install.user.js');
  assert.match(await page.locator('.notice').textContent(), /公式配布元から直接/);
  assert.equal(await page.locator('section[aria-labelledby="help-heading"] .license-help a').textContent(), 'モデルのライセンス・注意事項');
  assert.equal(await page.locator('.license-warning').evaluate(node => node.previousElementSibling.matches('section[aria-labelledby="storage-heading"]') && node.nextElementSibling.matches('section[aria-labelledby="model-heading"]')), true);
  assert.ok((await page.locator('a[href*="license"]').evaluateAll(links => links.map(link => link.href))).every(href => href === 'https://plamo.preferredai.jp/info/plamo-community-license-ja'));
  assert.equal(await page.locator('.license-warning li strong').count(), 1);
  assert.equal(await page.locator('.license-warning li strong').textContent(), '通常の訳文公表では、PLaMo による翻訳である旨の表示は不要です。');
  const licenseWarning = await page.locator('.license-warning').textContent();
  assert.match(licenseWarning, /書籍・論文.*全部または実質的な部分/);
  assert.match(licenseWarning, /訳文を使って学習したモデルには本契約の条件が適用/);
  assert.match(licenseWarning, /第三者の著作権/);
  await poll(page);
  assert.equal(posts(control).length, 0);
  control.setup.models.find(model => model.id === '2b').compatible = false;
  control.health.version = '1.0.2';
  await poll(page);
  assert.equal(await page.locator('#app-version').textContent(), 'v1.0.2');
  assert.equal(await row(page, '0.8b').locator('.recommended').isVisible(), true);
  assert.equal(await row(page, '2b').locator('.recommended').isVisible(), false);
  control.setup.models.find(model => model.id === '2b').compatible = true;
  await poll(page);
  await page.screenshot({ path: '.local/app-dashboard.png', fullPage: true });
  await page.close();
});

test('management displays the actual selected GPU and an honest CPU fallback without starting model work', async () => {
  const { page, control } = await fixture();
  try {
    control.health.engine = { backend: 'llama.cpp-native', loaded_model: '2b', state: 'idle', native: {
      device_index: 1, devices: [{ index: 0, type: 'cpu', name: 'CPU' }, { index: 1, type: 'gpu', name: 'Vulkan0', description: 'Test GPU' }], fallback_reason: '',
    } };
    await poll(page);
    assert.match(await page.locator('#engine-state').textContent(), /GPU · Test GPU/);
    assert.equal(await page.locator('#engine-detail').isHidden(), true);
    control.health.engine.native.device_index = -2;
    control.health.engine.native.fallback_reason = 'GPU model initialization failed';
    await poll(page);
    assert.match(await page.locator('#engine-state').textContent(), /CPU/);
    assert.doesNotMatch(await page.locator('#engine-state').textContent(), /Test GPU/);
    assert.equal(await page.locator('#engine-detail').isVisible(), true);
    assert.equal(await page.locator('#engine-detail').getAttribute('title'), 'GPU model initialization failed');
    control.health.engine = { backend: 'llama.cpp', loaded_model: '2b', state: 'idle', native_fallback_reason: 'Native runtime validation failed' };
    await poll(page);
    assert.match(await page.locator('#engine-state').textContent(), /CPU/);
    assert.equal(await page.locator('#engine-detail').getAttribute('title'), 'Native runtime validation failed');
    assert.deepEqual(posts(control), []);
  } finally { await page.close(); }
});

test('explicit download shows progress without selecting or loading the model, then explicit Use syncs only after ready', async () => {
  const { page, control } = await fixture();
  await page.evaluate(() => { window.chosenModels = []; document.addEventListener('cp-translator-model-selected', event => window.chosenModels.push(event.detail.model)); });
  const prepare = row(page, '0.8b').locator('[data-action="prepare"]');
  assert.match(await prepare.textContent(), /ダウンロード.*0\.57 GB/);
  await prepare.click();
  await page.locator('#download-title').filter({ hasText: 'ダウンロードしています' }).waitFor();
  assert.deepEqual(posts(control).map(request => ({ path: request.path, body: request.body, client: request.client })), [{ path: '/api/models/download', body: { model: '0.8b' }, client: 'setup' }]);
  assert.equal(await prepare.isDisabled(), true);
  await prepare.evaluate(button => button.click());
  assert.equal(posts(control).length, 1);
  assert.deepEqual(await page.evaluate(() => window.chosenModels), []);
  control.setup.download.bytes_received = 12 * 1024 ** 2;
  await poll(page);
  assert.match(await page.locator('#download-detail').textContent(), /12\.0 MiB/);
  assert.equal(await page.locator('#download-progress').isVisible(), true);
  assert.equal(await page.locator('#preparation').isHidden(), true);
  control.setup.download.stage = 'ready';
  control.setup.models.find(model => model.id === '0.8b').available = true;
  Object.assign(control.storage.models.find(model => model.id === '0.8b'), { downloaded: true, on_disk_bytes: 570000000 });
  await poll(page); await poll(page);
  assert.deepEqual(await page.evaluate(() => window.chosenModels), []);
  assert.equal(control.health.engine.loaded_model, null);
  assert.equal(control.setup.selected_model, '2b');
  assert.equal(await prepare.textContent(), 'このモデルを使う');
  await prepare.click();
  await page.locator('#state-title').filter({ hasText: 'モデルを読み込んでいます' }).waitFor();
  await page.waitForFunction(() => !document.querySelector('#cancel-download').disabled);
  assert.deepEqual(posts(control).map(request => request.path), ['/api/models/download', '/api/setup/prepare']);
  assert.deepEqual(await page.evaluate(() => window.chosenModels), []);
  await poll(page);
  assert.match(await page.locator('#engine-state').textContent(), /読み込み中/);
  control.setup.preparation.stage = 'ready';
  control.health.engine = { loaded_model: '0.8b', state: 'idle' };
  await poll(page); await poll(page);
  assert.deepEqual(await page.evaluate(() => window.chosenModels), ['0.8b']);
  assert.equal(await prepare.textContent(), '使用中');
  assert.equal(await row(page, '0.8b').locator('[data-action="unload"]').isVisible(), true);
  await page.close();
});

test('a cached model remains usable and unloadable during another download while conflicting file changes stay disabled', async () => {
  const { page, control } = await fixture();
  control.setup.models.find(model => model.id === '8b').compatible = true;
  await poll(page);
  await row(page, '0.8b').locator('[data-action="prepare"]').click();
  await page.locator('#download-title').filter({ hasText: 'ダウンロードしています' }).waitFor();
  assert.equal(await row(page, '0.8b').locator('[data-action="prepare"]').isDisabled(), true);
  assert.equal(await row(page, '8b').locator('[data-action="prepare"]').isDisabled(), true);
  assert.equal(await row(page, '2b').locator('[data-action="prepare"]').isDisabled(), false);
  assert.equal(await row(page, '2b').locator('[data-action="delete"]').isDisabled(), true);
  assert.equal(await page.locator('#choose-folder').isDisabled(), true);
  await row(page, '2b').locator('[data-action="prepare"]').click();
  await page.locator('#state-title').filter({ hasText: 'モデルを読み込んでいます' }).waitFor();
  await page.waitForFunction(() => !document.querySelector('#cancel-download').disabled);
  assert.equal(control.setup.download.stage, 'downloading');
  assert.deepEqual(posts(control).map(request => request.path), ['/api/models/download', '/api/setup/prepare']);
  control.setup.preparation.stage = 'ready'; control.health.engine = { state: 'idle', loaded_model: '2b' };
  await poll(page);
  assert.equal(await row(page, '2b').locator('[data-action="unload"]').isDisabled(), false);
  await row(page, '2b').locator('[data-action="unload"]').click();
  await page.getByText('モデルの読み込みを解除しました', { exact: true }).waitFor();
  assert.equal(control.health.engine.loaded_model, null);
  assert.equal(control.setup.download.stage, 'downloading');
  assert.deepEqual(posts(control).map(request => request.path), ['/api/models/download', '/api/setup/prepare', '/api/models/unload']);
  await page.close();
});

test('a new download can start during inference without changing the active model or allowing another inference action', async () => {
  const { page, control } = await fixture();
  control.health.engine = { state: 'translating', loaded_model: '2b' };
  Object.assign(control.setup.models.find(model => model.id === '8b'), { compatible: true, available: true });
  await poll(page);
  assert.equal(await row(page, '0.8b').locator('[data-action="prepare"]').isDisabled(), false);
  assert.equal(await row(page, '8b').locator('[data-action="prepare"]').isDisabled(), true);
  assert.equal(await row(page, '2b').locator('[data-action="unload"]').isDisabled(), true);
  await row(page, '0.8b').locator('[data-action="prepare"]').click();
  await page.locator('#download-title').filter({ hasText: 'ダウンロードしています' }).waitFor();
  assert.deepEqual(control.health.engine, { state: 'translating', loaded_model: '2b' });
  assert.match(await page.locator('#engine-state').textContent(), /翻訳中.*2B/);
  assert.deepEqual(posts(control).map(request => ({ path: request.path, body: request.body, client: request.client })), [{ path: '/api/models/download', body: { model: '0.8b' }, client: 'setup' }]);
  await page.close();
});

test('download cancellation disables duplicate cancel actions, keeps saved data and never selects the cancelled model', async () => {
  const { page, control } = await fixture();
  await page.evaluate(() => { window.chosenModels = []; document.addEventListener('cp-translator-model-selected', event => window.chosenModels.push(event.detail.model)); });
  await row(page, '0.8b').locator('[data-action="prepare"]').click();
  await page.locator('#download-title').filter({ hasText: 'ダウンロードしています' }).waitFor();
  let release; control.cancelWait = new Promise(resolve => { release = resolve; });
  const cancel = page.locator('#cancel-download'); await cancel.click();
  await page.waitForFunction(() => document.querySelector('#cancel-download').disabled);
  await poll(page);
  assert.equal(await cancel.isDisabled(), true);
  await cancel.evaluate(button => button.click());
  assert.equal(posts(control).filter(request => request.path.endsWith('/cancel')).length, 1);
  release();
  await page.locator('#download-title').filter({ hasText: 'ダウンロードを停止しました' }).waitFor();
  assert.match(await page.locator('#download-detail').textContent(), /取得済みのデータは残っています/);
  assert.equal(await cancel.isHidden(), true);
  assert.equal(await row(page, '0.8b').locator('[data-action="prepare"]').isDisabled(), false);
  assert.deepEqual(await page.evaluate(() => window.chosenModels), []);
  assert.equal(control.setup.selected_model, '2b');
  assert.deepEqual(posts(control).filter(request => request.path.endsWith('/cancel')).map(request => ({ body: request.body, client: request.client })), [{ body: {}, client: 'setup' }]);
  await page.close();
});

test('downloaded models can be used and unloaded while legacy31 is delete-only with confirmation', async () => {
  const { page, control } = await fixture();
  assert.equal(await row(page, '2b').locator('[data-action="prepare"]').textContent(), 'このモデルを使う');
  control.health.engine.loaded_model = '2b';
  control.storage.models.find(model => model.id === '31b').on_disk_bytes = 1024 ** 3;
  await poll(page);
  assert.equal(await row(page, '31b').locator('[data-action="prepare"]').isHidden(), true);
  assert.equal(await row(page, '31b').locator('[data-action="unload"]').isHidden(), true);
  await row(page, '2b').locator('[data-action="unload"]').click();
  await page.getByText('モデルの読み込みを解除しました', { exact: true }).waitFor();
  assert.match(await page.locator('#engine-state').textContent(), /未読み込み/);
  page.once('dialog', dialog => dialog.dismiss());
  await row(page, '31b').locator('[data-action="delete"]').click();
  assert.equal(posts(control).filter(request => request.path.includes('delete')).length, 0);
  page.once('dialog', dialog => dialog.accept());
  await row(page, '31b').locator('[data-action="delete"]').click();
  await page.getByText('31B の保存データを削除しました', { exact: true }).waitFor();
  assert.equal(await row(page, '31b').count(), 0);
  assert.deepEqual(posts(control).map(request => request.path), ['/api/models/unload', '/api/models/delete']);
  await page.close();
});

test('folder chooser waits for a user decision, cancellation is normal, and change preserves the old-folder explanation', async () => {
  const { page, control } = await fixture();
  let release;
  control.folderWait = new Promise(resolve => { release = resolve; });
  await page.locator('#choose-folder').click();
  await page.getByText('保存先を選んでください', { exact: true }).waitFor();
  assert.equal(await page.locator('#choose-folder').isDisabled(), true);
  await poll(page);
  assert.match(await page.locator('#choose-folder').textContent(), /選択中/);
  release();
  await page.getByText('保存先は変更していません', { exact: true }).waitFor();
  assert.equal(await page.locator('#folder').textContent(), control.storage.folder);
  const old = control.storage.folder;
  control.folderWait = null; control.folderResult = { folder: 'D:\\Models', models: [] };
  await page.locator('#choose-folder').click();
  await page.getByText('モデルの保存先を変更しました', { exact: true }).waitFor();
  assert.equal(await page.locator('#folder').textContent(), 'D:\\Models');
  assert.match(await page.locator('#previous-folder').textContent(), new RegExp(old.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(await page.locator('#storage-heading').locator('..').locator('..').textContent(), /保存先を変えても、取得済みモデルは自動移動しません/);
  assert.ok(posts(control).every(request => request.path === '/api/models/choose-folder'));
  await page.close();
});

test('cache capacity edits survive polling while retention stays fixed at7 days', async () => {
  const { page, control } = await fixture();
  assert.equal(await page.getByLabel('保存期間').count(), 0);
  assert.equal(await page.getByLabel('容量の上限').inputValue(), '1024');
  assert.deepEqual(await page.getByLabel('容量の上限').locator('option').allTextContents(), ['256 KiB', '512 KiB', '1 MiB', '2 MiB', '4 MiB', '8 MiB', '16 MiB', '32 MiB', '64 MiB']);
  await page.getByLabel('容量の上限').selectOption('256');
  await poll(page);
  assert.equal(await page.getByLabel('容量の上限').inputValue(), '256');
  await page.locator('#save-cache').click();
  await page.getByText('訳文キャッシュの設定を保存しました', { exact: true }).waitFor();
  assert.deepEqual(posts(control)[0].body, { max_kib: 256 });
  assert.equal(control.cache.retention_days, 7);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#clear-cache').click();
  await page.getByText('訳文キャッシュを消去しました', { exact: true }).waitFor();
  assert.match(await page.locator('#cache-stats').textContent(), /0 件 · 0 B/);
  assert.deepEqual(posts(control).map(request => request.path), ['/api/cache/policy', '/api/cache/clear']);
  await page.close();
});

test('a preserved legacy capacity is displayed without rounding or saving until a supported value is chosen', async () => {
  const { page, control } = await fixture();
  control.cache.max_bytes = 25 * 1024 ** 2;
  await poll(page);
  assert.equal(await page.getByLabel('容量の上限').inputValue(), '25600');
  assert.equal(await page.getByLabel('容量の上限').locator('option:checked').textContent(), '25 MiB（現在の設定）');
  assert.equal(await page.locator('#save-cache').isDisabled(), true);
  await poll(page);
  assert.equal(posts(control).length, 0);
  await page.getByLabel('容量の上限').selectOption('4096');
  await page.getByLabel('容量の上限').selectOption('25600');
  assert.equal(await page.locator('#save-cache').isDisabled(), true);
  await page.getByLabel('容量の上限').selectOption('4096');
  await page.locator('#save-cache').click();
  await page.getByText('訳文キャッシュの設定を保存しました', { exact: true }).waitFor();
  assert.deepEqual(posts(control)[0].body, { max_kib: 4096 });
  assert.equal(await page.getByLabel('容量の上限').inputValue(), '4096');
  assert.equal(await page.getByLabel('容量の上限').locator('option[value="25600"]').count(), 0);
  await page.close();
});

test('action errors persist across polling, and a connection loss disables actions until reconnected', async () => {
  const { page, control } = await fixture();
  control.actionError = '翻訳が終わってからお試しください。';
  await row(page, '0.8b').locator('[data-action="prepare"]').click();
  await page.getByText(control.actionError, { exact: true }).waitFor();
  await poll(page);
  assert.equal(await page.locator('#message-detail').textContent(), control.actionError);
  control.offline = true;
  await poll(page);
  assert.equal(await page.locator('#connection-error').isVisible(), true);
  assert.equal(await row(page, '0.8b').locator('[data-action="prepare"]').isDisabled(), true);
  assert.equal(await page.locator('#choose-folder').isDisabled(), true);
  control.offline = false;
  await poll(page);
  assert.equal(await row(page, '0.8b').locator('[data-action="prepare"]').isDisabled(), false);
  await page.close();
});

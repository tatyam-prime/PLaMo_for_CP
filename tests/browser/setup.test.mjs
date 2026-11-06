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
    setup: { application: 'plamo-for-cp', selected_model: '2b', device: { system: 'Windows', memory_gb: 16, supported: true }, models, preparation: { stage: 'idle', model: null, bytes_received: 0, error: null }, download: { stage: 'idle', model: null, bytes_received: 0, error: null } },
    health: { app: 'plamo-for-cp', engine: { loaded_model: null, state: 'idle' } },
    storage: { folder: 'C:\\Users\\競プロer\\Models', shared: false, models: [...models.map(model => ({ ...model, downloaded: model.available, on_disk_bytes: model.available ? 2 * 1024 ** 3 : 0 })), { id: '31b', on_disk_bytes: 0 }] },
    cache: { entries: 12, bytes: 512 * 1024, max_bytes: 1024 ** 2, retention_days: 7 },
  };
}

async function fixture() {
  const page = await browser.newPage();
  const control = { ...snapshot(), offline: false, actionError: null, requests: [], folderResult: null, folderWait: null, cancelWait: null };
  const html = await readFile(new URL('../../plamo_cp/assets/setup.html', import.meta.url));
  const script = await readFile(new URL('../../plamo_cp/assets/setup.js', import.meta.url));
  await page.route('http://127.0.0.1:8765/', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }));
  await page.route('http://127.0.0.1:8765/setup.js', route => route.fulfill({ contentType: 'application/javascript; charset=utf-8', body: script }));
  await page.route('http://127.0.0.1:8765/api/**', async route => {
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
    return route.fulfill({ status: path === '/api/models/download' ? 202 : 200, json: result });
  });
  await page.goto('http://127.0.0.1:8765/');
  await page.getByText('アプリに接続済み', { exact: true }).waitFor();
  return { page, control };
}
async function poll(page) { await page.evaluate(() => refresh()); }
const row = (page, model) => page.locator(`[data-model="${model}"]`);
const posts = control => control.requests.filter(request => request.method === 'POST');

test('dashboard shows actual storage, selected versus loaded state, and never starts downloads on render', async () => {
  const { page, control } = await fixture();
  assert.match(await page.title(), /アプリの管理/);
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
  assert.equal(await page.locator('.license-warning li strong').textContent(), '訳文を公表する際には，PLaMo 3 Translate により翻訳されたことを明示すること．');
  await poll(page);
  assert.equal(posts(control).length, 0);
  control.setup.models.find(model => model.id === '2b').compatible = false;
  await poll(page);
  assert.equal(await row(page, '0.8b').locator('.recommended').isVisible(), true);
  assert.equal(await row(page, '2b').locator('.recommended').isVisible(), false);
  control.setup.models.find(model => model.id === '2b').compatible = true;
  await poll(page);
  await page.screenshot({ path: '.local/app-dashboard.png', fullPage: true });
  await page.close();
});

test('explicit download shows progress without selecting or loading the model, then explicit Use syncs only after ready', async () => {
  const { page, control } = await fixture();
  await page.evaluate(() => { window.chosenModels = []; document.addEventListener('plamo-model-selected', event => window.chosenModels.push(event.detail.model)); });
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
  await page.evaluate(() => { window.chosenModels = []; document.addEventListener('plamo-model-selected', event => window.chosenModels.push(event.detail.model)); });
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

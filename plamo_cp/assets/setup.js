'use strict';

const elements = Object.fromEntries(['app-version', 'connection', 'connection-error', 'engine-state', 'selected-model', 'device', 'message', 'message-title', 'message-detail', 'folder', 'folder-type', 'choose-folder', 'open-folder', 'previous-folder', 'models', 'preparation', 'state-title', 'state-detail', 'progress', 'download', 'download-title', 'download-detail', 'download-progress', 'cancel-download', 'cache-stats', 'cache-form', 'cache-kib', 'save-cache', 'clear-cache'].map(id => [id, document.getElementById(id)]));
const cacheCapacitiesKiB = [256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536];
const modelNames = { '0.8b': '800M（0.8B）', '2b': '2B', '8b': '8B', '31b': '31B' };
const descriptions = { '0.8b': '省メモリ・速度を優先するときに', '2b': '標準モデル・品質と容量のバランス', '8b': 'メモリに余裕のあるパソコン向け' };
let state;
let connected = false;
let refreshTask = null;
let action = null;
let notice = null;
let cacheDirty = false;
const quietPercentages = ['100', '80', '64', '50', '40', '32', '25', '20'];
let pendingQuietLevel = null;
elements['quiet-level'] = document.getElementById('quiet-level');
let previousFolder = null;
let pendingModel = null;
const rows = new Map();
elements['engine-detail'] = document.getElementById('engine-detail');
const userscriptUpdate = document.getElementById('userscript-update');
const userscriptUpdateDetail = document.getElementById('userscript-update-detail');

function renderUserscriptUpdate() {
  const host = document.querySelector('#browser-settings > [data-cp-translator-ui]');
  const legacyHost = document.querySelector('#browser-settings > [data-plamo-ui]');
  const installed = host?.dataset.cpTranslatorUserscriptVersion;
  const available = state?.health.userscript_version;
  const comparison = compareUserscriptVersions(installed, available);
  // Versions through 0.4.15 mounted this same settings UI without a version
  // marker. An absent script still shows the installation instructions.
  const legacy = !host && legacyHost && document.documentElement.dataset.plamoUserscript === 'ready'
    && compareUserscriptVersions('0.4.15', available) === -1;
  userscriptUpdate.hidden = !connected || comparison !== -1 && !legacy;
  if (!userscriptUpdate.hidden) userscriptUpdateDetail.textContent = `${legacy ? `最新版は v${available} です。` : `現在 v${installed} → 最新 v${available}。`}更新後は、問題ページとこの画面を再読み込みしてください。`;
}

new MutationObserver(renderUserscriptUpdate).observe(document.getElementById('browser-settings'), { childList: true, attributes: true, subtree: true, attributeFilter: ['data-cp-translator-userscript-version'] });
document.addEventListener('cp-translator-browser-settings-ready', renderUserscriptUpdate);
document.addEventListener('plamo-browser-settings-ready', renderUserscriptUpdate);

async function call(path, body) {
  // A native folder picker deliberately has no time limit while the user chooses.
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-Plamo-Client': 'setup' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '操作を完了できませんでした。もう一度お試しください。');
  return result;
}

function size(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}

function inferenceBusy() {
  return !!action || (state && (!['idle', 'ready', 'error'].includes(state.setup.preparation.stage) || (state.health.engine.state || 'idle') !== 'idle'));
}
function downloading() { return !!state?.setup.download && !['idle', 'ready', 'error', 'cancelled'].includes(state.setup.download.stage); }
function isBusy() { return inferenceBusy() || downloading(); }

function createRow(id) {
  const row = document.createElement('div');
  row.className = 'model-row';
  row.dataset.model = id;
  row.innerHTML = '<div><div class="row-heading"><h3></h3><span class="tag recommended" hidden>おすすめ</span><span class="tag loaded" hidden>使用中</span></div><div class="model-description"></div></div><div><div class="model-status"></div><div class="model-size"></div></div><div class="actions"><button class="primary" data-action="prepare"></button><button data-action="unload">読み込みを解除</button><button class="danger" data-action="delete">削除</button></div>';
  row.querySelector('h3').textContent = modelNames[id] || id;
  row.querySelector('.model-description').textContent = descriptions[id] || '以前に取得したモデル（新規の利用はできません）';
  for (const button of row.querySelectorAll('button')) button.addEventListener('click', () => modelAction(id, button.dataset.action));
  elements.models.append(row);
  rows.set(id, row);
  return row;
}

function renderModels() {
  const publicModels = state.setup.models.filter(model => ['0.8b', '2b', '8b'].includes(model.id));
  const legacy = state.storage.models.filter(model => model.id === '31b' && model.on_disk_bytes > 0);
  const shown = [...publicModels, ...legacy];
  const recommended = publicModels.some(model => model.id === '2b' && model.compatible !== false) ? '2b' : '0.8b';
  if (!rows.size) elements.models.replaceChildren();
  for (const [id, row] of rows) if (!shown.some(model => model.id === id)) { row.remove(); rows.delete(id); }
  for (const model of shown) {
    const row = rows.get(model.id) || createRow(model.id);
    const stored = state.storage.models.find(item => item.id === model.id) || {};
    const loaded = state.health.engine.loaded_model === model.id;
    const available = model.available || stored.downloaded;
    const selected = state.setup.selected_model === model.id;
    const selectable = publicModels.some(item => item.id === model.id);
    const activeDownload = downloading() && state.setup.download.model === model.id;
    row.querySelector('.recommended').hidden = model.id !== recommended;
    row.querySelector('.loaded').hidden = !loaded;
    row.querySelector('.model-status').textContent = `${loaded ? 'メモリに読み込み済み' : available ? 'ダウンロード済み' : stored.on_disk_bytes > 0 ? '一部のデータを保存済み' : '未ダウンロード'}${selected ? ' · 選択中' : ''}`;
    row.querySelector('.model-size').textContent = `ディスク使用量 ${size(stored.on_disk_bytes)}${selectable ? ` ／ データ容量 約${model.weight_gb} GB` : ''}${model.compatible === false ? ` ／ ${model.min_memory_gb ? `メモリ ${model.min_memory_gb} GB 以上が必要です` : 'このパソコンでは利用できません'}` : ''}`;
    const prepare = row.querySelector('[data-action="prepare"]');
    prepare.hidden = !selectable;
    prepare.textContent = activeDownload ? 'ダウンロード中' : loaded ? '使用中' : available ? 'このモデルを使う' : `ダウンロード（約${model.weight_gb} GB）`;
    prepare.disabled = !connected || !!action || loaded || model.compatible === false || activeDownload || (available ? inferenceBusy() : downloading());
    const unload = row.querySelector('[data-action="unload"]');
    unload.hidden = !loaded || !selectable;
    unload.disabled = !connected || inferenceBusy() || activeDownload;
    const remove = row.querySelector('[data-action="delete"]');
    remove.hidden = !stored.on_disk_bytes;
    remove.disabled = !connected || isBusy();
  }
}

function render() {
  renderUserscriptUpdate();
  if (!state) return;
  const { setup, health, storage, cache } = state;
  elements['app-version'].hidden = !connected;
  elements['app-version'].textContent = typeof health.version === 'string' && health.version ? `v${health.version}` : 'バージョン不明';
  const loaded = health.engine.loaded_model;
  elements['engine-state'].textContent = health.engine.state === 'translating' ? `翻訳中 · ${modelNames[loaded] || loaded || ''}` : health.engine.state === 'loading' ? 'モデルを読み込み中' : loaded ? `利用できます · ${modelNames[loaded] || loaded}` : '待機中 · モデル未読み込み';
  const engine = health.engine, native = engine.native;
  const selectedDevice = Array.isArray(native?.devices) ? native.devices.find(device => device.index === native.device_index) : null;
  const target = engine.backend === 'mlx' ? 'GPU（Metal）'
    : engine.backend === 'llama.cpp-native' && selectedDevice && ['gpu', 'integrated_gpu'].includes(selectedDevice.type)
      ? 'GPU · ' + String(selectedDevice.description || selectedDevice.name || 'GPU').slice(0, 120)
      : ['llama.cpp', 'llama.cpp-native'].includes(engine.backend) ? 'CPU' : '';
  if (loaded && target) elements['engine-state'].textContent += ' · ' + target;
  const fallbackReason = engine.native_fallback_reason || (engine.backend === 'llama.cpp-native' && native?.fallback_reason) || '';
  elements['engine-detail'].hidden = !loaded || !fallbackReason;
  elements['engine-detail'].textContent = fallbackReason ? 'GPUを利用できなかったため、CPUで動作しています。' : '';
  elements['engine-detail'].title = typeof fallbackReason === 'string' ? fallbackReason : '';
  elements['selected-model'].textContent = modelNames[setup.selected_model] || setup.selected_model;
  const deviceName = { Darwin: 'Mac', Windows: 'Windows' }[setup.device.system] || setup.device.system;
  elements.device.textContent = `${deviceName} · メモリ ${setup.device.memory_gb} GB${setup.device.supported === false ? ' · 非対応' : ''}`;
  elements.folder.textContent = storage.folder;
  elements['folder-type'].textContent = storage.shared ? '共通のモデル保存先' : '指定した保存先';
  elements['choose-folder'].disabled = !connected || isBusy();
  elements['choose-folder'].textContent = action === 'choose-folder' ? 'フォルダーを選択中…' : '保存先を変更';
  elements['open-folder'].disabled = !connected || !!action;
  elements['previous-folder'].hidden = !previousFolder;
  elements['previous-folder'].textContent = previousFolder ? `変更前の保存先：${previousFolder}` : '';
  elements.message.hidden = !notice;
  elements.message.className = `status${notice?.error ? ' error' : notice?.success ? ' success' : ''}`;
  elements['message-title'].textContent = notice?.title || '';
  elements['message-detail'].textContent = notice?.detail || '';
  renderModels();
  const preparation = setup.preparation;
  const preparing = !['idle', 'ready', 'error'].includes(preparation.stage);
  elements.preparation.hidden = preparation.stage === 'idle' || (preparation.stage === 'ready' && !loaded);
  elements.preparation.className = `status${preparation.stage === 'error' ? ' error' : preparation.stage === 'ready' ? ' success' : ''}`;
  elements.progress.hidden = !preparing;
  const names = { queued: '準備を開始します', checking: '保存済みデータを確認しています', downloading: 'モデルをダウンロードしています', downloading_runtime: '翻訳エンジンをダウンロードしています', loading: 'モデルを読み込んでいます', ready: '翻訳に使える状態です', error: 'モデルの準備を完了できませんでした' };
  elements['state-title'].textContent = names[preparation.stage] || '';
  elements['state-detail'].textContent = preparation.stage === 'error' ? preparation.error || 'もう一度お試しください。' : preparation.stage.startsWith('downloading') ? `今回の取得量 ${size(preparation.bytes_received)}。アプリを開いたままお待ちください。` : preparation.stage === 'ready' ? `${modelNames[loaded] || loaded || ''} をメモリに読み込んでいます。ブラウザーから翻訳できます。` : 'アプリを開いたままお待ちください。';
  const download = setup.download || { stage: 'idle' }, downloadActive = downloading();
  const downloadName = modelNames[download.model] || download.model || '';
  elements.download.hidden = download.stage === 'idle';
  elements.download.className = `status${download.stage === 'error' ? ' error' : download.stage === 'ready' ? ' success' : ''}`;
  elements['download-title'].textContent = download.stage === 'ready' ? `${downloadName} のダウンロードが完了しました` : download.stage === 'error' ? `${downloadName} のダウンロードを完了できませんでした` : download.stage === 'cancelled' ? `${downloadName} のダウンロードを停止しました` : `${downloadName} をダウンロードしています`;
  elements['download-detail'].textContent = download.stage === 'error' ? download.error || 'もう一度お試しください。' : download.stage === 'ready' ? '使うときは、このモデルの「このモデルを使う」を押してください。' : download.stage === 'cancelled' ? '取得済みのデータは残っています。次回は続きから取得できます。' : `今回の取得量 ${size(download.bytes_received)}。取得済みの別モデルで翻訳できます。`;
  elements['download-progress'].hidden = !downloadActive;
  elements['cancel-download'].hidden = !downloadActive;
  elements['cancel-download'].disabled = !connected || !!action || download.stage === 'cancelling';
  elements['cache-stats'].textContent = `${cache.entries.toLocaleString()} 件 · ${size(cache.bytes)} / ${size(cache.max_bytes)}`;
  const currentKiB = cache.max_bytes / 1024;
  for (const option of elements['cache-kib'].querySelectorAll('[data-current-capacity]')) {
    if (Number(option.value) !== currentKiB) option.remove();
  }
  if (!cacheCapacitiesKiB.includes(currentKiB) && !Array.from(elements['cache-kib'].options).some(option => Number(option.value) === currentKiB)) {
    const option = document.createElement('option');
    option.value = String(currentKiB);
    option.textContent = `${currentKiB >= 1024 ? `${currentKiB / 1024} MiB` : `${currentKiB} KiB`}（現在の設定）`;
    option.dataset.currentCapacity = '';
    elements['cache-kib'].append(option);
  }
  if (!cacheDirty) elements['cache-kib'].value = String(currentKiB);
  for (const id of ['cache-kib', 'clear-cache']) elements[id].disabled = !connected || !!action;
  elements['save-cache'].disabled = !connected || !!action || !cacheDirty || !cacheCapacitiesKiB.includes(Number(elements['cache-kib'].value));
  const quietLevel = Number.isInteger(setup.quiet_level) && setup.quiet_level >= 0 && setup.quiet_level < quietPercentages.length ? setup.quiet_level : setup.quiet_mode === true ? 3 : 0;
  elements['quiet-level'].value = String(pendingQuietLevel ?? quietLevel);
  elements['quiet-level'].disabled = !connected || !!action;
}

async function refresh() {
  if (refreshTask) return refreshTask;
  refreshTask = fetchState();
  try { await refreshTask; } finally { refreshTask = null; }
}

async function fetchState() {
  try {
    const [setup, health, storage, cache] = await Promise.all(['/api/setup', '/api/health', '/api/models/storage', '/api/cache'].map(path => call(path)));
    if (setup.application !== 'plamo-for-cp' || health.app !== 'plamo-for-cp') throw new Error('別のアプリが応答しました。');
    state = { setup, health, storage, cache };
    if (pendingModel && setup.preparation.stage === 'ready' && health.engine.loaded_model === pendingModel) {
      document.dispatchEvent(new CustomEvent('cp-translator-model-selected', { detail: { model: pendingModel } }));
      pendingModel = null;
    }
    if (setup.preparation.stage === 'error') pendingModel = null;
    connected = true;
    elements.connection.textContent = 'アプリに接続済み';
    elements.connection.classList.add('online');
    elements['connection-error'].hidden = true;
  } catch {
    connected = false;
    elements.connection.textContent = 'アプリに未接続';
    elements.connection.classList.remove('online');
    elements['connection-error'].hidden = false;
    for (const button of document.querySelectorAll('button, select')) button.disabled = true;
  } finally { render(); }
}

async function runAction(name, operation) {
  if (action || !connected) return;
  action = name;
  notice = name === 'choose-folder' ? { title: '保存先を選んでください', detail: 'フォルダー選択画面で決定するか、キャンセルしてください。' } : null;
  render();
  try { await operation(); } catch (error) { notice = { title: '操作を完了できませんでした', detail: error.message || '通信状態を確認して、もう一度お試しください。', error: true }; }
  finally {
    // An earlier poll may have read the pre-operation state. Complete it, then
    // fetch a fresh snapshot before enabling a second management action.
    if (refreshTask) await refreshTask;
    await refresh();
    action = null;
    render();
  }
}

async function modelAction(id, name) {
  const row = rows.get(id);
  const button = row?.querySelector(`[data-action="${name}"]`);
  if (!button || button.disabled || button.hidden) return;
  if (name === 'delete' && !confirm(`${modelNames[id] || id} の保存データを削除しますか？\n使用中の場合は読み込みも解除します。再利用するにはダウンロードが必要です。`)) return;
  await runAction(`${name}:${id}`, async () => {
    const model = state.setup.models.find(item => item.id === id), stored = state.storage.models.find(item => item.id === id);
    const downloadOnly = name === 'prepare' && !(model?.available || stored?.downloaded);
    await call(downloadOnly ? '/api/models/download' : name === 'prepare' ? '/api/setup/prepare' : `/api/models/${name}`, { model: id });
    if (name === 'prepare' && !downloadOnly) pendingModel = id;
    if (name !== 'prepare') notice = { title: name === 'delete' ? `${modelNames[id] || id} の保存データを削除しました` : 'モデルの読み込みを解除しました', detail: name === 'unload' ? 'ディスク上のデータは残っています。使うときにもう一度読み込めます。' : '', success: true };
  });
}

elements['cancel-download'].addEventListener('click', () => runAction('cancel-download', () => call('/api/models/download/cancel', {})));

elements['choose-folder'].addEventListener('click', () => runAction('choose-folder', async () => {
  const old = state.storage.folder;
  const result = await call('/api/models/choose-folder', {});
  if (result.cancelled) { notice = { title: '保存先は変更していません' }; return; }
  if (result.folder !== old) previousFolder = old;
  notice = { title: result.folder === old ? '現在と同じ保存先を選びました' : 'モデルの保存先を変更しました', detail: '取得済みモデルは自動移動しません。元のファイルは元のフォルダーに残っています。', success: true };
}));
elements['open-folder'].addEventListener('click', () => runAction('open-folder', () => call('/api/models/open-folder', {})));
elements['quiet-level'].addEventListener('change', async () => {
  if (elements['quiet-level'].disabled) return;
  const level = Number(elements['quiet-level'].value);
  if (!Number.isInteger(level) || level < 0 || level >= quietPercentages.length) { render(); return; }
  pendingQuietLevel = level;
  try {
    await runAction('performance', async () => {
      await call('/api/performance', { quiet_level: level });
      notice = { title: `推論の稼働率を${quietPercentages[level]}%にしました`, success: true };
    });
  } finally { pendingQuietLevel = null; render(); }
});
elements['cache-kib'].addEventListener('change', () => { cacheDirty = Number(elements['cache-kib'].value) * 1024 !== state.cache.max_bytes; render(); });
elements['cache-form'].addEventListener('submit', event => {
  event.preventDefault();
  if (elements['save-cache'].disabled) return;
  runAction('cache-policy', async () => {
    await call('/api/cache/policy', { max_kib: Number(elements['cache-kib'].value) });
    cacheDirty = false;
    notice = { title: '訳文キャッシュの設定を保存しました', success: true };
  });
});
elements['clear-cache'].addEventListener('click', () => {
  if (elements['clear-cache'].disabled || !confirm('保存済みの訳文キャッシュをすべて消去しますか？\nモデルのデータは削除しません。')) return;
  runAction('clear-cache', async () => { await call('/api/cache/clear', {}); notice = { title: '訳文キャッシュを消去しました', success: true }; });
});

refresh();
setInterval(refresh, 2000);

'use strict';

const ui = Object.fromEntries(['model', 'prefetch', 'source-language', 'document-type', 'translate', 'stop', 'progress', 'model-info', 'status', 'source', 'clear', 'copy', 'results'].map(id => [id, document.getElementById(id)]));
let batchPreference = 16, setup = null, modelChosen = false, busy = false, generation = 0, controller = null, pipeline = null;
let segments = [];
const outputs = new Map(), rows = new Map();
for (const value of TRANSLATION_BATCH_SIZES) {
  const option = document.createElement('option'); option.value = String(value); option.textContent = String(value); ui.prefetch.append(option);
}
function readPreferences() {
  batchPreference = 16;
  try { const value = JSON.parse(localStorage.getItem('cp-translator-batch-size')); if (TRANSLATION_BATCH_SIZES.includes(value)) batchPreference = value; } catch {}
  try {
    if (localStorage.getItem('cp-translator-batch-size') !== String(batchPreference)) localStorage.setItem('cp-translator-batch-size', String(batchPreference));
  } catch {}
}
readPreferences();
ui.prefetch.value = String(batchPreference);
function preferredBatch() { return batchPreference; }
function status(message, error = false) { ui.status.textContent = message; ui.status.classList.toggle('error', error); }
function render() {
  const model = setup?.models.find(item => item.id === ui.model.value);
  ui.model.disabled = busy || !setup; ui.prefetch.disabled = busy || !setup; ui.prefetch.value = preferredBatch();
  ui['source-language'].disabled = busy; ui['document-type'].disabled = busy;
  ui.translate.disabled = busy || !ui.source.value.trim() || !model?.available || model.compatible === false;
  ui.stop.hidden = !busy; ui.progress.hidden = !busy;
  ui.copy.disabled = !outputs.size;
  ui['model-info'].textContent = !setup ? 'アプリに接続できません。PLaMo for 競プロ アプリを開いてください。' : model?.compatible === false ? 'このモデルはこのパソコンでは利用できません。' : !model?.available ? 'このモデルは未取得です。設定・管理からダウンロードしてください。' : setup.supported_batch_sizes?.length <= 1 || !setup.supported_batch_sizes?.includes(preferredBatch()) ? 'この環境では並列翻訳を利用できず、1つずつ翻訳します。' : '大きくすると、一度に行う計算量はほぼ比例して増え、メモリ使用量は少し増えます。個々の翻訳は少し遅くなりますが、全体の翻訳を効率よく進められます。標準は16です。';
}
function clearDrafts() { for (const segment of segments) { const row = rows.get(segment.id); if (row?.classList.contains('draft')) { row.textContent = segment.original; row.className = 'paragraph pending'; row.removeAttribute('title'); } } }
function cancel(message) {
  generation++; pipeline?.cancel(); pipeline = null; controller?.abort(); controller = null; busy = false;
  clearDrafts();
  if (message) status(message); render();
}
function clearResults() { segments = []; outputs.clear(); rows.clear(); ui.results.replaceChildren(); const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '訳文がここに表示されます。'; ui.results.append(empty); }
function splitSource(value) {
  const slots = new Map(), used = new Set(value.match(/\[\[PLM_\d+\]\]/g) || []); let index = 0;
  const protectedText = value.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~|\${6}[\s\S]*?\${6}|\$\$\$[\s\S]*?\$\$\$|\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|(?<!\\)\$(?!\s)[^$\n]+?\$/g, text => { let token; do { token = `[[PLM_${index++}]]`; } while (used.has(token)); used.add(token); slots.set(token, text); return token; });
  const chunks = [];
  const paragraphLimit = setup?.supported_translation_previews === true ? 600 : 300;
  const sentences = new Intl.Segmenter(ui['source-language'].value === 'Chinese' ? 'zh' : 'en', { granularity: 'sentence' });
  const parts = [];
  for (const paragraph of protectedText.split(/\n[ \t]*\n/).map(text => text.trim()).filter(Boolean)) {
    if (paragraph.length <= paragraphLimit) { parts.push(paragraph); continue; }
    let part = '';
    for (const { segment } of sentences.segment(paragraph)) {
      if (part && part.length + segment.length > paragraphLimit) { parts.push(part.trim()); part = ''; }
      part += segment;
    }
    if (part.trim()) parts.push(part.trim());
  }
  for (let paragraph of parts) {
    while (paragraph.length > 1800) {
      const prefix = paragraph.slice(0, 1800); let end = Math.max(prefix.lastIndexOf('\n'), prefix.lastIndexOf(' '));
      if (end < 900) end = 1800;
      const open = paragraph.lastIndexOf('[[PLM_', end); if (open >= 0 && paragraph.indexOf(']]', open) + 2 > end) end = open || paragraph.indexOf(']]', open) + 2;
      if (/[\uDC00-\uDFFF]/.test(paragraph[end]) && /[\uD800-\uDBFF]/.test(paragraph[end - 1])) end--;
      chunks.push(paragraph.slice(0, end).trim()); paragraph = paragraph.slice(end).trimStart();
    }
    if (paragraph) chunks.push(paragraph);
  }
  return chunks.map((text, index) => ({ id: `text-${index}`, text, slots, original: restore(text, slots), reference: text.replace(/\[\[PLM_\d+\]\]/g, ' '), translatable: /[A-Za-z]{2}|\p{Script=Han}/u.test(text.replace(/\[\[PLM_\d+\]\]/g, '')) }));
}
function restore(text, slots) { return text.replace(/\[\[PLM_\d+\]\]/g, token => slots.get(token) ?? token); }
function configuredGlossary() {
  let saved = null;
  try { const raw = localStorage.getItem('cp-translator-glossary'); if (raw !== null) { const value = JSON.parse(raw); if (Array.isArray(value) && value.every(pair => pair && typeof pair.source === 'string' && typeof pair.target === 'string')) saved = value; } } catch {}
  const defaults = Array.isArray(setup?.default_glossary) ? setup.default_glossary : [];
  if (ui['document-type'].value === 'problem') return saved ?? defaults;
  return (saved ?? []).filter(pair => !defaults.some(term => term.source.toLowerCase() === pair.source.toLowerCase() && term.target === pair.target));
}
async function json(response) {
  let value; try { value = await response.json(); } catch { throw new Error('アプリの応答を読み取れませんでした。'); }
  if (!response.ok) { const error = new Error(typeof value.detail === 'string' ? value.detail : '処理を完了できませんでした。'); error.status = response.status; throw error; }
  return value;
}
async function refreshModels() {
  try { const value = await json(await fetch('/api/setup')); if (value.application !== 'plamo-for-cp') throw new Error(); setup = value;
    for (const option of ui.model.options) option.disabled = !setup.models.some(model => model.id === option.value && model.compatible !== false);
    if (!modelChosen && !busy && setup.models.some(model => model.id === setup.selected_model)) ui.model.value = setup.selected_model;
  } catch { setup = null; } render();
}
async function translate() {
  if (busy || ui.translate.disabled) return;
  const token = ++generation, model = ui.model.value, wanted = preferredBatch();
  const batch = setup.supported_batch_sizes?.includes(wanted) ? wanted : 1;
  segments = splitSource(ui.source.value); outputs.clear(); rows.clear(); ui.results.replaceChildren();
  for (const segment of segments) { const row = document.createElement('p'); row.className = 'paragraph pending'; row.dataset.segment = segment.id; row.textContent = segment.translatable ? '翻訳待ち…' : segment.original; rows.set(segment.id, row); ui.results.append(row); if (!segment.translatable) outputs.set(segment.id, segment.original); }
  const work = segments.filter(segment => segment.translatable);
  if (!work.length) { status('翻訳する文章がありません。数式・コード・数値はそのまま表示します。'); render(); return; }
  busy = true; controller = new AbortController(); const requestController = controller;
  ui.progress.max = work.length; ui.progress.value = 0; render(); let failed = 0;
  try {
    pipeline = startTranslationPipeline({ segments: work.map(({ id, text }) => ({ id, text })), batchSize: batch,
      fields: { model, precision: '4bit', source_lang: ui['source-language'].value, target_lang: 'Japanese', document_type: ui['document-type'].value, glossary: configuredGlossary() },
      async request(path, body, options = {}) { const data = body === undefined ? undefined : JSON.stringify(body); return json(await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-Plamo-Client': 'userscript' }, body: data, signal: options.keepalive ? undefined : requestController.signal, keepalive: options.keepalive === true && (data === undefined || new TextEncoder().encode(data).length < 60000) })); },
      onPreview(event) { if (token !== generation || outputs.has(event.id)) return; const segment = work.find(item => item.id === event.id), row = rows.get(event.id); if (!segment || !row) return;
        const text = event.text.replace(/\[\[(?:P(?:L(?:M(?:_\d*)?)?)?)?\]?$/, '');
        if (!text || (text.match(/\[\[PLM_\d+\]\]/g) || []).some(marker => !segment.text.includes(marker))) return;
        row.textContent = restore(text, segment.slots) + '…'; row.className = 'paragraph draft'; row.title = '翻訳中';
      },
      onResult(event) { if (token !== generation) return; const segment = work.find(item => item.id === event.id), row = rows.get(event.id); if (!segment || !row) return;
        const error = event.type === 'segment_error'; const text = error ? segment.original : restore(event.text, segment.slots);
        outputs.set(event.id, text); row.textContent = text; row.className = error ? 'paragraph failed' : 'paragraph translation'; row.removeAttribute('title');
        if (error) { failed++; row.title = event.message || 'この段落は原文を保持しています。'; }
        ui.copy.disabled = !outputs.size;
      },
      onProgress({ completed, total }) { if (token === generation) { ui.progress.value = completed; status(`${completed} / ${total} 段落を翻訳中…`); } },
    });
    await pipeline.promise; if (token !== generation) return;
    status(`${work.length - failed} / ${work.length} 段落を翻訳しました。${failed ? `${failed}段落は原文を保持しています。` : ''}`, failed > 0);
  } catch (error) { if (token === generation) status(error.name === 'AbortError' ? '停止しました。' : error.message, error.name !== 'AbortError'); }
  finally { if (token === generation) { clearDrafts(); pipeline = null; controller = null; busy = false; render(); } }
}
ui.source.addEventListener('input', () => { cancel(); clearResults(); status('原文を貼り付けて「翻訳」を押してください。'); render(); });
ui.clear.addEventListener('click', () => { cancel(); ui.source.value = ''; clearResults(); status('原文を貼り付けて「翻訳」を押してください。'); render(); ui.source.focus(); });
ui.translate.addEventListener('click', translate);
ui.stop.addEventListener('click', () => cancel('停止しました。完了した訳文は残っています。'));
ui.model.addEventListener('change', () => { modelChosen = true; render(); });
ui.prefetch.addEventListener('change', () => { const value = Number(ui.prefetch.value); if (!TRANSLATION_BATCH_SIZES.includes(value)) { status('翻訳の並列数を候補から選んでください。', true); render(); return; } batchPreference = value; try { localStorage.setItem('cp-translator-batch-size', String(value)); window.dispatchEvent(new Event('cp-translator-batch-size-changed')); } catch {} render(); });
ui.copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(segments.filter(segment => outputs.has(segment.id)).map(segment => outputs.get(segment.id)).join('\n\n')); status('訳文をコピーしました。'); } catch { status('コピーできませんでした。訳文を選択してコピーしてください。', true); } });
window.addEventListener('storage', event => { if (event.key === 'cp-translator-batch-size' || event.key === null) { readPreferences(); render(); } });
window.addEventListener('focus', refreshModels);
window.addEventListener('pagehide', () => cancel());
refreshModels();

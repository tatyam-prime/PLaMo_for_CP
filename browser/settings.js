function renderBrowserSettings(target) {
  const host = document.createElement('div'); host.dataset.cpTranslatorUi = '';
  host.dataset.cpTranslatorUserscriptVersion = USERSCRIPT_VERSION;
  const shadow = host.attachShadow({ mode: 'open' });
  const make = (tag, text, attrs = {}) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); return node; };
  const style = make('style');
  style.textContent = ':host{display:block;font:13px/1.55 system-ui,sans-serif;color:#25384b;color-scheme:light}*{box-sizing:border-box}h2{margin:0 0 12px;font-size:17px}h3{margin:0 0 9px;font-size:14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.sites{display:grid;grid-template-columns:1fr 1fr;gap:7px}label.check,.row,.entry,.custom-site{display:flex;align-items:center;gap:7px}.row{flex-wrap:wrap;margin:10px 0}.help,.message{font-size:11px;color:#657a90;margin:7px 0}.message:empty{display:none}input,select,textarea,button{font:inherit;color:inherit}input[type=checkbox]{accent-color:#315fce}button,select,input[type=text],textarea{border:1px solid #bdcad8;border-radius:5px;background:white;padding:5px 7px}button{font-size:12px;cursor:pointer;white-space:nowrap}button:hover{background:#eef3fb}.entry input{flex:1;min-width:0}.custom-site{font-size:11px;margin:7px 0}.custom-site label{flex:1;min-width:0;overflow-wrap:anywhere;display:flex;gap:5px}.custom-site button{padding:3px 5px;font-size:11px}textarea{display:block;width:100%;margin-top:5px;resize:vertical;font:12px/1.5 ui-monospace,monospace}.full{margin-top:16px}a{color:#315fce}[hidden]{display:none!important}@media(max-width:650px){.grid{grid-template-columns:1fr}}';
  const heading = make('h2', 'このブラウザーの翻訳設定', { id: 'browser-heading' });
  const grid = make('div', undefined, { class: 'grid' }), siteSection = make('section'), preferences = make('section');
  siteSection.append(make('h3', '翻訳ボタンを表示するサイト'));
  const visibilityActions = make('div', undefined, { class: 'column-actions', role: 'group', 'aria-label': 'サイトの表示を一括変更' });
  const allVisible = make('input', undefined, { type: 'checkbox', 'aria-label': 'すべてのサイトの表示' });
  const visibilityLabel = make('label', undefined, { class: 'check' });
  visibilityLabel.append(allVisible, document.createTextNode('すべて')); visibilityActions.append(visibilityLabel);
  const automaticActions = make('div', undefined, { class: 'column-actions', role: 'group', 'aria-label': 'サイトの自動翻訳を一括変更' });
  const allAutomatic = make('input', undefined, { type: 'checkbox', 'aria-label': 'すべてのサイトの自動翻訳' });
  const automaticLabel = make('label', undefined, { class: 'check' });
  automaticLabel.append(allAutomatic, document.createTextNode('すべて')); automaticActions.append(automaticLabel);
  style.textContent += '.site-table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px}.site-table th,.site-table td{border-bottom:1px solid #e5ebf1;padding:6px 3px;text-align:center}.site-table th:first-child,.site-table td:first-child{text-align:left;overflow-wrap:anywhere}.site-table thead th{font-weight:500;font-size:11px;color:#657a90;vertical-align:top}.site-table td:first-child{max-width:180px}tr.custom-site{display:table-row;margin:0}.site-actions{display:flex;gap:3px}.site-actions button{padding:3px 5px;font-size:11px}.column-actions{display:flex;justify-content:center;margin-top:5px}.column-actions label.check{gap:3px;font-size:10px;line-height:1.35;cursor:pointer}';
  const table = make('table', undefined, { class: 'site-table' }), head = make('thead'), headRow = make('tr');
  for (const text of ['サイト', '表示', '自動翻訳', '操作']) {
    const cell = make('th', text, { scope: 'col' });
    if (text === '表示') cell.append(visibilityActions);
    if (text === '自動翻訳') cell.append(automaticActions);
    headRow.append(cell);
  }
  head.append(headRow);
  const sites = make('tbody', undefined, { id: 'sites' });
  const builtins = SITE_REGISTRY;
  const autoSettings = () => readPreference('cp-translator-auto-sites', {});
  const saveAutomatic = (id, checked) => writePreference('cp-translator-auto-sites', { ...autoSettings(), [id]: checked });
  const checkCell = input => { const cell = make('td'); cell.append(input); return cell; };
  const checks = new Map();
  for (const site of builtins) {
    const { id: key, name } = site;
    const row = make('tr', undefined, { 'data-site': key }), input = make('input', undefined, { type: 'checkbox', 'aria-label': `${name} の表示` }), automatic = make('input', undefined, { type: 'checkbox', 'aria-label': `${name} の自動翻訳` });
    input.addEventListener('change', () => {
      const disabled = new Set(readPreference('cp-translator-disabled-sites', [])); if (input.checked) disabled.delete(key); else disabled.add(key);
      writePreference('cp-translator-disabled-sites', [...disabled]);
    });
    automatic.addEventListener('change', () => saveAutomatic(key, automatic.checked));
    const description = make('td'); description.append(make('a', name, { href: site.homepage, target: '_blank', rel: 'noopener noreferrer' }));
    if (site.notice) {
      const note = make('p', site.notice.text + ' ', { class: 'help' }); note.append(make('a', site.notice.label, { href: site.notice.url, target: '_blank', rel: 'noopener noreferrer' })); description.append(note);
    }
    row.append(description, checkCell(input), checkCell(automatic), make('td')); sites.append(row); checks.set(key, { input, automatic });
  }
  const custom = make('tbody', undefined, { id: 'custom-sites' }), form = make('form'), inputLabel = make('label', 'サイトを追加', { for: 'site-input' });
  table.append(head, sites, custom);
  const entry = make('div', undefined, { class: 'entry' }), input = make('input', undefined, { id: 'site-input', type: 'text', placeholder: 'example.com', required: '', autocomplete: 'off', spellcheck: 'false' });
  const saveSite = make('button', '追加', { type: 'submit' }), cancel = make('button', '取消', { type: 'button', hidden: '' });
  entry.append(input, saveSite, cancel); form.append(inputLabel, entry);
  const siteMessage = make('p', '', { class: 'message', role: 'status', id: 'sites-message' });
  siteSection.append(table, make('p', '「表示」が有効なサイトだけで自動翻訳します。モデルは自動取得しません。対応ジャッジの問題文は専用の設定を使い、追加サイトの重複は上の行を優先します。', { class: 'help' }), form, make('p', 'example.com または https://example.com/articles/* の形で追加できます。変更後は対象のページを再読み込みしてください。', { class: 'help' }), siteMessage);
  let editing;
  const siteList = () => { const value = readPreference('cp-translator-custom-sites', []); return Array.isArray(value) ? value : []; };
  function setAllVisible(enabled) {
    const disabled = new Set(readPreference('cp-translator-disabled-sites', []));
    for (const site of builtins) if (enabled) disabled.delete(site.id); else disabled.add(site.id);
    writePreference('cp-translator-disabled-sites', [...disabled]);
    writePreference('cp-translator-custom-sites', siteList().map(site => ({ ...site, enabled })));
    siteMessage.textContent = `すべてのサイトの表示を${enabled ? 'ON' : 'OFF'}にしました。対象のページを再読み込みしてください。`;
  }
  allVisible.addEventListener('change', () => setAllVisible(allVisible.checked));
  function setAllAutomatic(enabled) {
    const automatic = { ...autoSettings() };
    for (const site of [...builtins, ...siteList()]) automatic[site.id] = enabled;
    writePreference('cp-translator-auto-sites', automatic);
    siteMessage.textContent = `すべての登録サイトの自動翻訳を${enabled ? 'ON' : 'OFF'}にしました。自動翻訳するには「表示」もONにしてください。`;
  }
  allAutomatic.addEventListener('change', () => setAllAutomatic(allAutomatic.checked));
  function resetEditor() { editing = undefined; input.value = ''; saveSite.textContent = '追加'; cancel.hidden = true; }
  function renderCustomSites() {
    custom.replaceChildren();
    for (const site of siteList()) {
      const row = make('tr', undefined, { class: 'custom-site', 'data-site': site.id }), enabled = make('input', undefined, { type: 'checkbox', 'aria-label': `${site.pattern} の表示` }), automatic = make('input', undefined, { type: 'checkbox', 'aria-label': `${site.pattern} の自動翻訳` }); enabled.checked = site.enabled;
      automatic.checked = autoSettings()?.[site.id] === true; automatic.disabled = !site.enabled;
      enabled.addEventListener('change', () => writePreference('cp-translator-custom-sites', siteList().map(item => item.id === site.id ? { ...item, enabled: enabled.checked } : item)));
      automatic.addEventListener('change', () => saveAutomatic(site.id, automatic.checked));
      const edit = make('button', '編集', { type: 'button', 'aria-label': `${site.pattern} を編集` });
      edit.addEventListener('click', () => { editing = site.id; input.value = site.pattern; saveSite.textContent = '変更を保存'; cancel.hidden = false; input.focus(); });
      const remove = make('button', '削除', { type: 'button', 'aria-label': `${site.pattern} を削除` });
      remove.addEventListener('click', () => { writePreference('cp-translator-custom-sites', siteList().filter(item => item.id !== site.id)); const automatic = { ...autoSettings() }; delete automatic[site.id]; writePreference('cp-translator-auto-sites', automatic); if (editing === site.id) resetEditor(); siteMessage.textContent = 'サイトを削除しました。'; });
      const actions = make('td'), actionRow = make('div', undefined, { class: 'site-actions' }); actionRow.append(edit, remove); actions.append(actionRow);
      const destination = make('td');
      try { const rule = siteRule(site.pattern); const url = rule.pattern.includes('*') ? rule.origin.replace('://*.', '://').replace(/\/\*$/, '/') : rule.pattern; destination.append(make('a', site.pattern, { href: url, target: '_blank', rel: 'noopener noreferrer' })); }
      catch { destination.textContent = site.pattern; }
      row.append(destination, checkCell(enabled), checkCell(automatic), actions); custom.append(row);
    }
  }
  cancel.addEventListener('click', resetEditor);
  form.addEventListener('submit', event => {
    event.preventDefault();
    try {
      const rule = siteRule(input.value), list = siteList();
      if (list.some(item => item.id !== editing && item.pattern === rule.pattern)) throw new Error('このサイトはすでに登録されています。');
      if (!editing && list.length >= 32) throw new Error('追加できるサイトは32件までです。');
      const next = editing ? list.map(item => item.id === editing ? { ...item, pattern: rule.pattern, enabled: true } : item) : [...list, { id: crypto.randomUUID(), pattern: rule.pattern, enabled: true }];
      writePreference('cp-translator-custom-sites', next); resetEditor(); siteMessage.textContent = 'サイトを保存しました。対象のページを再読み込みしてください。';
    } catch (error) { siteMessage.textContent = error.message; }
  });
  preferences.append(make('h3', '翻訳の並列数'));
  const batchRow = make('div', undefined, { class: 'row' }), batch = make('select', undefined, { id: 'batch-size' });
  for (const value of TRANSLATION_BATCH_SIZES) batch.append(make('option', String(value), { value: String(value) }));
  batch.addEventListener('change', () => {
    const count = Number(batch.value), supported = readPreference('cp-translator-supported-batch-sizes', [1]);
    if (TRANSLATION_BATCH_SIZES.includes(count) && Array.isArray(supported) && supported.includes(count)) writePreference('cp-translator-batch-size', count);
    else refreshSettings();
  });
  batchRow.append(make('label', '翻訳の並列数', { for: 'batch-size' }), batch); preferences.append(batchRow);
  const batchHelp = make('p', '', { class: 'help', id: 'batch-help' });
  preferences.append(batchHelp);
  grid.append(siteSection, preferences);
  const glossarySection = make('section', undefined, { class: 'full' }), glossary = make('textarea', undefined, { id: 'glossary', rows: '12', spellcheck: 'false', placeholder: '原語 = 訳語' });
  let glossaryDirty = false; glossary.addEventListener('input', () => { glossaryDirty = true; });
  const glossaryMessage = make('p', '', { class: 'message', role: 'status', id: 'glossary-message' }), saveGlossary = make('button', '用語集を保存', { id: 'save-glossary', type: 'button' });
  saveGlossary.addEventListener('click', () => {
    try {
      const pairs = glossary.value.split('\n').filter(line => line.trim()).map((line, index) => { const separator = line.indexOf('='); if (separator <= 0 || !line.slice(separator + 1).trim()) throw new Error(`${index + 1}行目を「原語 = 訳語」の形で入力してください。`); return { source: line.slice(0, separator).trim(), target: line.slice(separator + 1).trim() }; });
      if (pairs.length > 64 || pairs.reduce((sum, pair) => sum + pair.source.length + pair.target.length, 0) > 6000) throw new Error('用語集は64件・合計6000文字以内にしてください。');
      writePreference('cp-translator-glossary', pairs); glossaryDirty = false; glossaryMessage.textContent = `用語集を保存しました（${pairs.length}件）。`;
    } catch (error) { glossaryMessage.textContent = error.message; }
  });
  const glossaryActions = make('div', undefined, { class: 'row' }); glossaryActions.append(saveGlossary, make('span', '1行に 原語 = 訳語。最大64件・合計6000文字。空にして保存すると用語集を使いません。一般文書では初期語を除き、追加・変更した語だけを使います。', { class: 'help' }));
  glossarySection.append(make('label', '用語集', { for: 'glossary' }), glossary, glossaryActions, glossaryMessage);
  const reset = make('button', '設定をデフォルトに戻す', { id: 'reset-browser-settings', type: 'button' });
  const resetMessage = make('p', '', { id: 'settings-reset-message', class: 'message', role: 'status' });
  reset.addEventListener('click', () => {
    resetBrowserPreferences(); resetEditor(); glossaryDirty = false;
    siteMessage.textContent = glossaryMessage.textContent = '';
    refreshSettings(); resetMessage.textContent = 'ブラウザーの設定をデフォルトに戻しました。';
  });
  const resetRow = make('div', undefined, { class: 'row' });
  resetRow.append(reset, make('span', 'モデルファイルと訳文キャッシュは残します。', { class: 'help' }));
  shadow.append(style, heading, grid, glossarySection, resetRow, resetMessage, make('p', '設定は Tampermonkey に保存し、このブラウザーのタブ間で共有します。一般ページは Tampermonkey のメニュー「このページを翻訳」から使えます。', { class: 'help' }));
  target.removeAttribute('aria-labelledby');
  target.setAttribute('aria-label', 'このブラウザーの翻訳設定');
  target.replaceChildren(host);
  function refreshSettings() {
    const disabled = readPreference('cp-translator-disabled-sites', []); for (const [key, controls] of checks) { controls.input.checked = !disabled.includes(key); controls.automatic.checked = autoSettings()?.[key] === true; controls.automatic.disabled = disabled.includes(key); }
    const customSites = siteList(), automatic = autoSettings();
    const aggregate = (input, values) => {
      const count = values.filter(Boolean).length;
      input.checked = count > 0 && count === values.length;
      input.indeterminate = count > 0 && count < values.length;
    };
    aggregate(allVisible, [...checks.values()].map(controls => controls.input.checked).concat(customSites.map(site => site.enabled)));
    aggregate(allAutomatic, [...builtins, ...customSites].map(site => automatic?.[site.id] === true));
    const supported = readPreference('cp-translator-supported-batch-sizes', [1]), requested = readPreference('cp-translator-batch-size', 16);
    const size = TRANSLATION_BATCH_SIZES.includes(requested) ? requested : 16;
    for (const option of batch.options) option.disabled = !supported.includes(Number(option.value));
    batch.value = supported.includes(size) ? String(size) : '1';
    batchHelp.textContent = supported.length > 1 ? '大きくすると、一度に行う計算量はほぼ比例して増え、メモリ使用量は少し増えます。個々の翻訳は少し遅くなりますが、全体の翻訳を効率よく進められます。標準は16です。' : 'この環境では並列翻訳を利用できず、1つずつ翻訳します。';
    renderCustomSites();
    if (!glossaryDirty) glossary.value = readPreference('cp-translator-glossary', []).map(pair => `${pair.source} = ${pair.target}`).join('\n');
  }
  refreshSettings(); document.addEventListener('cp-translator-preferences-changed', refreshSettings);
}

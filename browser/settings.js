function renderBrowserSettings(target) {
  const host = document.createElement('div'); host.dataset.plamoUi = '';
  const shadow = host.attachShadow({ mode: 'open' });
  const make = (tag, text, attrs = {}) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); return node; };
  const style = make('style');
  style.textContent = ':host{display:block;font:13px/1.55 system-ui,sans-serif;color:#25384b;color-scheme:light}*{box-sizing:border-box}h2{margin:0 0 12px;font-size:17px}h3{margin:0 0 9px;font-size:14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.sites{display:grid;grid-template-columns:1fr 1fr;gap:7px}label.check,.row,.entry,.custom-site{display:flex;align-items:center;gap:7px}.row{flex-wrap:wrap;margin:10px 0}.help,.message{font-size:11px;color:#657a90;margin:7px 0}.message:empty{display:none}input,select,textarea,button{font:inherit;color:inherit}input[type=checkbox]{accent-color:#315fce}button,select,input[type=text],textarea{border:1px solid #bdcad8;border-radius:5px;background:white;padding:5px 7px}button{font-size:12px;cursor:pointer;white-space:nowrap}button:hover{background:#eef3fb}.entry input{flex:1;min-width:0}.custom-site{font-size:11px;margin:7px 0}.custom-site label{flex:1;min-width:0;overflow-wrap:anywhere;display:flex;gap:5px}.custom-site button{padding:3px 5px;font-size:11px}textarea{display:block;width:100%;margin-top:5px;resize:vertical;font:12px/1.5 ui-monospace,monospace}.full{margin-top:16px}a{color:#315fce}[hidden]{display:none!important}@media(max-width:650px){.grid{grid-template-columns:1fr}}';
  const heading = make('h2', 'このブラウザーの翻訳設定', { id: 'browser-heading' });
  const grid = make('div', undefined, { class: 'grid' }), siteSection = make('section'), preferences = make('section');
  siteSection.append(make('h3', '翻訳ボタンを表示するサイト'));
  style.textContent += '.site-table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px}.site-table th,.site-table td{border-bottom:1px solid #e5ebf1;padding:6px 3px;text-align:center}.site-table th:first-child,.site-table td:first-child{text-align:left;overflow-wrap:anywhere}.site-table thead th{font-weight:500;font-size:11px;color:#657a90}.site-table td:first-child{max-width:180px}tr.custom-site{display:table-row;margin:0}.site-actions{display:flex;gap:3px}.site-actions button{padding:3px 5px;font-size:11px}';
  const table = make('table', undefined, { class: 'site-table' }), head = make('thead'), headRow = make('tr');
  for (const text of ['サイト', '表示', '自動翻訳', '操作']) headRow.append(make('th', text, { scope: 'col' }));
  head.append(headRow);
  const sites = make('tbody', undefined, { id: 'sites' });
  const builtins = SITE_REGISTRY;
  const autoSettings = () => readPreference('plamo-auto-sites', {});
  const saveAutomatic = (id, checked) => writePreference('plamo-auto-sites', { ...autoSettings(), [id]: checked });
  const checkCell = input => { const cell = make('td'); cell.append(input); return cell; };
  const checks = new Map();
  for (const site of builtins) {
    const { id: key, name } = site;
    const row = make('tr', undefined, { 'data-site': key }), input = make('input', undefined, { type: 'checkbox', 'aria-label': `${name} の表示` }), automatic = make('input', undefined, { type: 'checkbox', 'aria-label': `${name} の自動翻訳` });
    input.addEventListener('change', () => {
      const disabled = new Set(readPreference('plamo-disabled-sites', [])); if (input.checked) disabled.delete(key); else disabled.add(key);
      writePreference('plamo-disabled-sites', [...disabled]);
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
  const siteList = () => { const value = readPreference('plamo-custom-sites', []); return Array.isArray(value) ? value : []; };
  function resetEditor() { editing = undefined; input.value = ''; saveSite.textContent = '追加'; cancel.hidden = true; }
  function renderCustomSites() {
    custom.replaceChildren();
    for (const site of siteList()) {
      const row = make('tr', undefined, { class: 'custom-site', 'data-site': site.id }), enabled = make('input', undefined, { type: 'checkbox', 'aria-label': `${site.pattern} の表示` }), automatic = make('input', undefined, { type: 'checkbox', 'aria-label': `${site.pattern} の自動翻訳` }); enabled.checked = site.enabled;
      automatic.checked = autoSettings()?.[site.id] === true; automatic.disabled = !site.enabled;
      enabled.addEventListener('change', () => writePreference('plamo-custom-sites', siteList().map(item => item.id === site.id ? { ...item, enabled: enabled.checked } : item)));
      automatic.addEventListener('change', () => saveAutomatic(site.id, automatic.checked));
      const edit = make('button', '編集', { type: 'button', 'aria-label': `${site.pattern} を編集` });
      edit.addEventListener('click', () => { editing = site.id; input.value = site.pattern; saveSite.textContent = '変更を保存'; cancel.hidden = false; input.focus(); });
      const remove = make('button', '削除', { type: 'button', 'aria-label': `${site.pattern} を削除` });
      remove.addEventListener('click', () => { writePreference('plamo-custom-sites', siteList().filter(item => item.id !== site.id)); const automatic = { ...autoSettings() }; delete automatic[site.id]; writePreference('plamo-auto-sites', automatic); if (editing === site.id) resetEditor(); siteMessage.textContent = 'サイトを削除しました。'; });
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
      writePreference('plamo-custom-sites', next); resetEditor(); siteMessage.textContent = 'サイトを保存しました。対象のページを再読み込みしてください。';
    } catch (error) { siteMessage.textContent = error.message; }
  });
  preferences.append(make('h3', '同時に翻訳する段落数'));
  const batchSelectors = new Map();
  for (const model of ['0.8b', '2b', '8b']) {
    const batchRow = make('div', undefined, { class: 'row' }), batch = make('select', undefined, { id: 'batch-size-' + model });
    for (let value = 1; value <= 16; value++) batch.append(make('option', value + '段落', { value: String(value) }));
    batch.addEventListener('change', () => {
      const count = Number(batch.value), supported = readPreference('plamo-supported-batch-sizes', [1]);
      if (Number.isInteger(count) && count >= 1 && count <= 16 && Array.isArray(supported) && supported.includes(count)) writePreference('plamo-batch-sizes', { ...readPreference('plamo-batch-sizes', {}), [model]: count });
      else refreshSettings();
    });
    batchRow.append(make('label', `${model.toUpperCase()} の段落数`, { for: 'batch-size-' + model }), batch); preferences.append(batchRow); batchSelectors.set(model, batch);
  }
  const batchHelp = make('p', '', { class: 'help', id: 'batch-help' });
  preferences.append(batchHelp);
  grid.append(siteSection, preferences);
  const glossarySection = make('section', undefined, { class: 'full' }), glossary = make('textarea', undefined, { id: 'glossary', rows: '11', spellcheck: 'false', placeholder: '原語 = 訳語' });
  let glossaryDirty = false; glossary.addEventListener('input', () => { glossaryDirty = true; });
  const glossaryMessage = make('p', '', { class: 'message', role: 'status', id: 'glossary-message' }), saveGlossary = make('button', '用語集を保存', { id: 'save-glossary', type: 'button' });
  saveGlossary.addEventListener('click', () => {
    try {
      const pairs = glossary.value.split('\n').filter(line => line.trim()).map((line, index) => { const separator = line.indexOf('='); if (separator <= 0 || !line.slice(separator + 1).trim()) throw new Error(`${index + 1}行目を「原語 = 訳語」の形で入力してください。`); return { source: line.slice(0, separator).trim(), target: line.slice(separator + 1).trim() }; });
      if (pairs.length > 64 || pairs.reduce((sum, pair) => sum + pair.source.length + pair.target.length, 0) > 6000) throw new Error('用語集は64件・合計6000文字以内にしてください。');
      writePreference('plamo-glossary', pairs); writePreference('plamo-glossary-version', 1); glossaryDirty = false; glossaryMessage.textContent = `用語集を保存しました（${pairs.length}件）。`;
    } catch (error) { glossaryMessage.textContent = error.message; }
  });
  const glossaryActions = make('div', undefined, { class: 'row' }); glossaryActions.append(saveGlossary, make('span', '1行に 原語 = 訳語。最大64件・合計6000文字。空にして保存すると用語集を使いません。一般文書では初期語を除き、追加・変更した語だけを使います。', { class: 'help' }));
  glossarySection.append(make('label', '用語集', { for: 'glossary' }), glossary, glossaryActions, glossaryMessage);
  const reset = make('button', '設定をデフォルトに戻す', { id: 'reset-browser-settings', type: 'button' });
  const resetMessage = make('p', '', { id: 'settings-reset-message', class: 'message', role: 'status' });
  reset.addEventListener('click', () => {
    for (const key of ['plamo-text-prefetch', 'plamo-pdf-prefetch', 'plamo-text-use-context']) localStorage.removeItem(key);
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
    const disabled = readPreference('plamo-disabled-sites', []); for (const [key, controls] of checks) { controls.input.checked = !disabled.includes(key); controls.automatic.checked = autoSettings()?.[key] === true; controls.automatic.disabled = disabled.includes(key); }
    const supported = readPreference('plamo-supported-batch-sizes', [1]), requested = readPreference('plamo-batch-sizes', {});
    for (const [model, batch] of batchSelectors) {
      for (const option of batch.options) option.disabled = !supported.includes(Number(option.value));
      const size = Number(requested?.[model] ?? { '0.8b': 4, '2b': 4, '8b': 8 }[model]);
      batch.value = supported.includes(size) ? String(size) : '1';
    }
    batchHelp.textContent = supported.length > 1 ? '少しずつ翻訳を開始し、設定した段落数まで同時に進めます。完成した部分から表示します。このページの未完了段落数の上限です。ほかのページの依頼とはサーバー側でまとめて処理します。' : 'この環境では1段落ずつ翻訳します。';
    renderCustomSites();
    if (!glossaryDirty) glossary.value = readPreference('plamo-glossary', []).map(pair => `${pair.source} = ${pair.target}`).join('\n');
  }
  refreshSettings(); document.addEventListener('plamo-preferences-changed', refreshSettings);
}

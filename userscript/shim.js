(() => {
  'use strict';
  if (globalThis.__plamoUserscriptStarted) return;
  globalThis.__plamoUserscriptStarted = true;
  const API = '__PLAMO_API__';
  const MODELS = ['0.8b', '2b', '8b'];
  const keys = ['plamo-model', 'plamo-pending-model', 'plamo-disabled-sites', 'plamo-panel-position', 'plamo-panel-offset', 'plamo-glossary', 'plamo-default-glossary', 'plamo-glossary-version', 'plamo-custom-sites', 'plamo-auto-sites', 'plamo-batch-sizes', 'plamo-supported-batch-sizes', 'plamo-translation-previews'];
  const nativePage = location.origin === API && ['/', '/text'].includes(location.pathname);
  __PLAMO_SITE_RULES__
  __PLAMO_SITE_REGISTRY__
  __PLAMO_TRANSLATION_JOBS__
  const builtinSites = SITE_REGISTRY;
  const builtin = () => builtinSites.find(site => site.host.test(location.hostname));
  const matchingCustomSite = () => { const sites = GM_getValue('plamo-custom-sites', []); return Array.isArray(sites) ? sites.find(site => site.enabled && siteMatches(site.pattern, location.href)) : undefined; };
  const isCustomSite = () => { const sites = GM_getValue('plamo-custom-sites', []); return Array.isArray(sites) && sites.some(site => site.enabled && siteMatches(site.pattern, location.href)); };
  const currentSite = () => {
    const site = builtin();
    if (site?.documentType === 'general' && matchingCustomSite()) return matchingCustomSite().id;
    if (problemRoots(site).length) return GM_getValue('plamo-disabled-sites', []).includes(site.id) ? null : site.id;
    return matchingCustomSite()?.id || null;
  };
  // Replace the former global switch once, without overriding explicit choices.
  if (GM_getValue('plamo-auto-sites-migrated', false) !== true) {
    const automatic = { ...GM_getValue('plamo-auto-sites', {}) };
    if (GM_getValue('plamo-auto-translate', false) === true) {
      const disabled = GM_getValue('plamo-disabled-sites', []);
      for (const { id, documentType } of builtinSites) if (documentType !== 'general' && !disabled.includes(id) && automatic[id] === undefined) automatic[id] = true;
      for (const site of GM_getValue('plamo-custom-sites', [])) if (site.enabled && automatic[site.id] === undefined) automatic[site.id] = true;
      GM_setValue('plamo-auto-sites', automatic);
    }
    GM_setValue('plamo-auto-sites-migrated', true);
    GM_deleteValue('plamo-auto-translate');
  }
  const automatic = GM_getValue('plamo-auto-sites', {}), retiredSites = ['spoj', 'dmoj'];
  if (retiredSites.some(id => Object.hasOwn(automatic, id))) { const cleaned = { ...automatic }; for (const id of retiredSites) delete cleaned[id]; GM_setValue('plamo-auto-sites', cleaned); }
  const disabledSites = GM_getValue('plamo-disabled-sites', []);
  if (retiredSites.some(id => disabledSites.includes(id))) GM_setValue('plamo-disabled-sites', disabledSites.filter(id => !retiredSites.includes(id)));
  const readPreference = (key, fallback) => key === 'plamo-custom-site' ? isCustomSite() : key === 'plamo-current-site' ? currentSite() : GM_getValue(key, fallback);
  function publishGlossary() {
    if (!nativePage) return;
    const glossary = GM_getValue('plamo-glossary', null);
    if (!Array.isArray(glossary)) return;
    try {
      const serialized = JSON.stringify(glossary);
      if (localStorage.getItem('plamo-glossary') !== serialized) localStorage.setItem('plamo-glossary', serialized);
    } catch {}
  }
  function initializeGlossary(defaults) {
    if (!Array.isArray(defaults) || defaults.some(pair => typeof pair?.source !== 'string' || typeof pair?.target !== 'string')) return;
    const previousDefaults = GM_getValue('plamo-default-glossary', null);
    const current = GM_getValue('plamo-glossary', null);
    const samePairs = (left, right) => Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((pair, index) => typeof pair?.source === 'string' && typeof right[index]?.source === 'string' && pair.source.toLowerCase() === right[index].source.toLowerCase() && pair.target === right[index].target);
    if (GM_getValue('plamo-glossary-version', 0) === 1 && Array.isArray(current) && current.length && Array.isArray(previousDefaults) && previousDefaults.length
      && samePairs(current, previousDefaults) && !samePairs(previousDefaults, defaults)) {
      GM_setValue('plamo-glossary', defaults);
    }
    if (GM_getValue('plamo-glossary-version', 0) !== 1) {
      const saved = GM_getValue('plamo-glossary', []), overrides = new Map();
      if (Array.isArray(saved)) for (const pair of saved) if (typeof pair?.source === 'string' && typeof pair?.target === 'string' && pair.source.trim() && pair.target.trim()) overrides.set(pair.source.toLowerCase(), { source: pair.source, target: pair.target });
      const missing = defaults.filter(pair => !overrides.has(pair.source.toLowerCase()));
      // Preserve the former custom list before using remaining space for defaults.
      let count = overrides.size, characters = [...overrides.values()].reduce((sum, pair) => sum + pair.source.length + pair.target.length, 0);
      const included = new Set();
      for (const pair of missing) if (count < 64 && characters + pair.source.length + pair.target.length <= 6000) { included.add(pair.source.toLowerCase()); count++; characters += pair.source.length + pair.target.length; }
      const merged = defaults.filter(pair => included.has(pair.source.toLowerCase()) || overrides.has(pair.source.toLowerCase())).map(pair => overrides.get(pair.source.toLowerCase()) || pair);
      const defaultKeys = new Set(defaults.map(pair => pair.source.toLowerCase()));
      merged.push(...[...overrides].filter(([key]) => !defaultKeys.has(key)).map(([, pair]) => pair));
      GM_setValue('plamo-glossary', merged); GM_setValue('plamo-glossary-version', 1);
    }
    GM_setValue('plamo-default-glossary', defaults);
    publishGlossary();
  }
  const notifyPreferences = () => { publishGlossary(); document.dispatchEvent(new Event('plamo-preferences-changed')); };
  const writePreference = (key, value) => { GM_setValue(key, value); notifyPreferences(); };
  function resetBrowserPreferences() {
    const defaults = {
      'plamo-auto-sites': {}, 'plamo-disabled-sites': [], 'plamo-custom-sites': [],
      'plamo-model': '2b', 'plamo-glossary': GM_getValue('plamo-default-glossary', []),
      'plamo-batch-sizes': { '0.8b': 4, '2b': 4, '8b': 8 },
      'plamo-panel-position': 'right-bottom', 'plamo-panel-offset': null,
    };
    for (const [key, value] of Object.entries(defaults)) GM_setValue(key, value);
    GM_setValue('plamo-glossary-version', 1);
    for (const key of ['plamo-pending-model', 'plamo-auto-translate', 'plamo-use-history', 'plamo-use-source-context', 'plamo-batch-size']) GM_deleteValue(key);
    notifyPreferences();
  }
  for (const key of keys) GM_addValueChangeListener(key, notifyPreferences);
  const savedModel = GM_getValue('plamo-model', null);
  if (savedModel && !MODELS.includes(savedModel)) GM_setValue('plamo-model', '2b');
  if (GM_getValue('plamo-pending-model', null) && !MODELS.includes(GM_getValue('plamo-pending-model'))) GM_deleteValue('plamo-pending-model');
  const openManagement = () => GM_openInTab(API + '/#browser-settings', { active: true, insert: true });
  const openText = () => GM_openInTab(API + '/text', { active: true, insert: true });
  const commands = new Map();
  const registerCommand = (label, callback) => commands.set(label, callback);
  const requests = new Set();
  function requestHTTP(details) {
    const url = new URL(details.url);
    const method = details.method || 'GET';
    const jobPath = /^\/api\/translation-jobs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(url.pathname);
    const cancelPath = /^\/api\/translation-jobs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/cancel$/i.test(url.pathname);
    const cursor = url.searchParams.get('after');
    const poll = method === 'GET' && jobPath && /^\?after=(0|[1-9]\d?)$/.test(url.search) && Number(cursor) <= 64;
    const ordinary = !url.search && (method === 'GET' && ['/api/setup', '/api/health'].includes(url.pathname)
      || method === 'POST' && ['/api/translate', '/api/translation-jobs'].includes(url.pathname) || method === 'POST' && cancelPath);
    if (url.origin !== API || url.username || url.password || url.hash
      || !(poll || ordinary)
      || details.data !== undefined && (method !== 'POST' || typeof details.data !== 'string' || details.data.length > 256000)) throw new Error('Unsupported local translation request');
    if (cancelPath && details.data !== '{}') throw new Error('Unsupported cancellation request');
    let handle, timer, finished = false;
    function finish(kind, response) {
      if (finished) return; finished = true; clearTimeout(timer); requests.delete(cancel);
      const callback = { load: details.onload, abort: details.onabort, timeout: details.ontimeout, error: details.onerror }[kind]; callback?.(response);
    }
    function cancel() { if (finished) return; handle?.abort(); finish('abort'); }
    if (!details.keepalive) requests.add(cancel);
    timer = setTimeout(() => { if (!finished) { finish('timeout'); handle?.abort(); } }, details.timeout || 60000);
    try {
      handle = GM_xmlhttpRequest({ method, url: url.href, data: details.data, timeout: details.timeout || 60000,
        headers: { 'Content-Type': 'application/json', 'X-Plamo-Client': 'userscript' }, anonymous: true, redirect: 'error',
        onload(response) {
          if (response.finalUrl && new URL(response.finalUrl).origin !== API) { finish('error'); return; }
          finish('load', response);
        }, onabort: response => finish('abort', response), ontimeout: response => finish('timeout', response), onerror: response => finish('error', response) });
    } catch { finish('error'); }
    return { abort: cancel };
  }
  window.addEventListener('pagehide', () => { for (const cancel of [...requests]) cancel(); });
  async function setupPreferences() {
    const setup = await new Promise(resolve => requestHTTP({ method: 'GET', url: API + '/api/setup', timeout: 2500,
      onload(response) { try { const setup = JSON.parse(response.responseText); resolve(response.status === 200 && setup.application === 'plamo-for-cp' ? setup : null); } catch { resolve(null); } }, onerror: () => resolve(null), onabort: () => resolve(null), ontimeout: () => resolve(null) }));
    if (!setup) return null;
    initializeGlossary(setup.default_glossary);
    const pending = GM_getValue('plamo-pending-model', null);
    if (pending && setup.preparation?.stage === 'ready' && setup.preparation.model === pending) { GM_setValue('plamo-model', pending); GM_deleteValue('plamo-pending-model'); }
    if (!GM_getValue('plamo-model', null) && MODELS.includes(setup.selected_model)) GM_setValue('plamo-model', setup.selected_model);
    if (Array.isArray(setup.models)) GM_setValue('plamo-model-weights', Object.fromEntries(setup.models.filter(model => Number.isFinite(model.weight_gb) && model.weight_gb > 0).map(model => [model.id, model.weight_gb.toFixed(2)])));
    GM_setValue('plamo-supported-batch-sizes', Array.isArray(setup.supported_batch_sizes) ? setup.supported_batch_sizes.filter(size => Number.isInteger(size) && size >= 1 && size <= 16) : [1]);
    GM_setValue('plamo-translation-jobs', setup.supported_translation_jobs === true);
    GM_setValue('plamo-translation-previews', setup.supported_translation_previews === true);
    return setup;
  }
  let starting, started = false;
  function ensureCore() {
    if (started) return Promise.resolve();
    if (starting) return starting;
    starting = setupPreferences().then(() => {
      if (started) return; started = true;
      __PLAMO_TRANSLATOR__
    }).finally(() => { starting = null; });
    return starting;
  }
  GM_registerMenuCommand('このページを翻訳', () => ensureCore().then(() => commands.get('PLaMo: ページ全体を翻訳')?.()));
  GM_registerMenuCommand('テキストを翻訳', openText);
  GM_registerMenuCommand('設定・管理', openManagement);
  __PLAMO_BROWSER_SETTINGS__
  if (nativePage && location.pathname === '/text') { setupPreferences().then(publishGlossary); return; }
  if (location.origin === API && location.pathname === '/') {
    setupPreferences().then(setup => {
      if (!setup) return;
      const mount = () => {
        const target = document.querySelector('#browser-settings'); if (!target) return false;
        renderBrowserSettings(target);
        document.documentElement.dataset.plamoUserscript = 'ready';
        document.addEventListener('plamo-model-selected', event => { const model = event.detail?.model; if (MODELS.includes(model)) writePreference('plamo-model', model); });
        document.dispatchEvent(new Event('plamo-browser-settings-ready'));
        return true;
      };
      if (!mount()) { const observer = new MutationObserver(() => { if (mount()) observer.disconnect(); }); observer.observe(document.documentElement, { childList: true, subtree: true }); }
    });
    return;
  }
  const configured = () => {
    if (translationRestriction()) return false;
    const site = builtin();
    if (site) {
      if (site.documentType === 'general' && site.problemPath && !site.problemPath.test(location.pathname)) return isCustomSite();
      const disabled = GM_getValue('plamo-disabled-sites', []).includes(site.id);
      if (site.documentType === 'general' && matchingCustomSite()) return true;
      if (!disabled) return true;
      if (problemRoots(site).length) return false;
    }
    return isCustomSite();
  };
  if (configured()) ensureCore();
  GM_addValueChangeListener('plamo-custom-sites', () => { if (!started && configured()) ensureCore(); });
  if (builtin()?.id === 'repovive') {
    const resume = () => { if (!started && configured()) ensureCore(); };
    new MutationObserver(resume).observe(document, { childList: true, subtree: true, characterData: true });
    window.addEventListener('popstate', resume);
  }
})();

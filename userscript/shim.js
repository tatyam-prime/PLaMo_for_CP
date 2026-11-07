(() => {
  'use strict';
  if (globalThis.__cpTranslatorUserscriptStarted) return;
  globalThis.__cpTranslatorUserscriptStarted = true;
  const API = '__TRANSLATOR_API__';
  const USERSCRIPT_VERSION = '__TRANSLATOR_USERSCRIPT_VERSION__';
  const MODELS = ['0.8b', '2b', '8b'];
  const keys = ['cp-translator-model', 'cp-translator-pending-model', 'cp-translator-disabled-sites', 'cp-translator-panel-position', 'cp-translator-panel-offset', 'cp-translator-glossary', 'cp-translator-default-glossary', 'cp-translator-custom-sites', 'cp-translator-auto-sites', 'cp-translator-batch-size', 'cp-translator-supported-batch-sizes', 'cp-translator-translation-previews'];
  const nativePage = location.origin === API && ['/', '/text'].includes(location.pathname);
  __TRANSLATOR_TRANSLATION_JOBS__
  const validBatchSize = value => TRANSLATION_BATCH_SIZES.includes(value);
  if (!validBatchSize(GM_getValue('cp-translator-batch-size', null))) GM_setValue('cp-translator-batch-size', 16);
  for (const [key, value] of [['cp-translator-auto-sites', {}], ['cp-translator-disabled-sites', []], ['cp-translator-custom-sites', []]]) {
    if (GM_getValue(key, null) === null) GM_setValue(key, value);
  }
  __TRANSLATOR_SITE_RULES__
  __TRANSLATOR_SITE_REGISTRY__
  __TRANSLATOR_USERSCRIPT_VERSION_HELPER__
  const builtinSites = SITE_REGISTRY;
  const builtin = () => builtinSites.find(site => site.host.test(location.hostname));
  const matchingCustomSite = () => { const sites = GM_getValue('cp-translator-custom-sites', []); return Array.isArray(sites) ? sites.find(site => site.enabled && siteMatches(site.pattern, location.href)) : undefined; };
  const isCustomSite = () => { const sites = GM_getValue('cp-translator-custom-sites', []); return Array.isArray(sites) && sites.some(site => site.enabled && siteMatches(site.pattern, location.href)); };
  const currentSite = () => {
    const site = builtin();
    if (site?.documentType === 'general' && matchingCustomSite()) return matchingCustomSite().id;
    if (problemRoots(site).length) return GM_getValue('cp-translator-disabled-sites', []).includes(site.id) ? null : site.id;
    return matchingCustomSite()?.id || null;
  };
  const readPreference = (key, fallback) => key === 'cp-translator-custom-site' ? isCustomSite() : key === 'cp-translator-current-site' ? currentSite() : GM_getValue(key, fallback);
  function publishGlossary() {
    if (!nativePage) return;
    const glossary = GM_getValue('cp-translator-glossary', null);
    if (!Array.isArray(glossary)) return;
    try {
      const serialized = JSON.stringify(glossary);
      if (localStorage.getItem('cp-translator-glossary') !== serialized) localStorage.setItem('cp-translator-glossary', serialized);
    } catch {}
  }
  function publishBatchSize() {
    if (!nativePage) return;
    try {
      const value = GM_getValue('cp-translator-batch-size', 16), serialized = String(validBatchSize(value) ? value : 16);
      const previous = localStorage.getItem('cp-translator-batch-size');
      if (previous !== serialized) {
        localStorage.setItem('cp-translator-batch-size', serialized);
        window.dispatchEvent(new StorageEvent('storage', { key: 'cp-translator-batch-size', oldValue: previous, newValue: serialized, storageArea: localStorage }));
      }
    } catch {}
  }
  function initializeGlossary(defaults) {
    if (!Array.isArray(defaults) || defaults.some(pair => typeof pair?.source !== 'string' || typeof pair?.target !== 'string')) return;
    if (GM_getValue('cp-translator-glossary', null) === null) GM_setValue('cp-translator-glossary', defaults);
    GM_setValue('cp-translator-default-glossary', defaults);
    publishGlossary();
  }
  const notifyPreferences = () => { publishGlossary(); publishBatchSize(); document.dispatchEvent(new Event('cp-translator-preferences-changed')); };
  const writePreference = (key, value) => { GM_setValue(key, value); notifyPreferences(); };
  function resetBrowserPreferences() {
    const defaults = {
      'cp-translator-auto-sites': {}, 'cp-translator-disabled-sites': [], 'cp-translator-custom-sites': [],
      'cp-translator-model': '2b', 'cp-translator-glossary': GM_getValue('cp-translator-default-glossary', []),
      'cp-translator-batch-size': 16,
      'cp-translator-panel-position': 'right-bottom', 'cp-translator-panel-offset': null,
    };
    for (const [key, value] of Object.entries(defaults)) GM_setValue(key, value);
    GM_deleteValue('cp-translator-pending-model');
    notifyPreferences();
  }
  for (const key of keys) GM_addValueChangeListener(key, notifyPreferences);
  const savedModel = GM_getValue('cp-translator-model', null);
  if (savedModel && !MODELS.includes(savedModel)) GM_setValue('cp-translator-model', '2b');
  if (GM_getValue('cp-translator-pending-model', null) && !MODELS.includes(GM_getValue('cp-translator-pending-model'))) GM_deleteValue('cp-translator-pending-model');
  if (nativePage) {
    publishBatchSize();
    window.addEventListener('cp-translator-batch-size-changed', () => {
      try {
        const value = JSON.parse(localStorage.getItem('cp-translator-batch-size'));
        if (validBatchSize(value)) writePreference('cp-translator-batch-size', value);
      } catch {}
    });
  }
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
  function readLocalState(path, { signal } = {}) {
    return new Promise(resolve => {
      if (signal?.aborted) { resolve(null); return; }
      let handle;
      const cancel = () => handle?.abort();
      const finish = value => { signal?.removeEventListener('abort', cancel); resolve(signal?.aborted ? null : value); };
      handle = requestHTTP({ method: 'GET', url: API + path, timeout: 1800,
        onload(response) { try { finish(response.status === 200 ? JSON.parse(response.responseText) : null); } catch { finish(null); } },
        onerror: () => finish(null), onabort: () => finish(null), ontimeout: () => finish(null) });
      signal?.addEventListener('abort', cancel, { once: true });
      if (signal?.aborted) cancel();
    });
  }
  async function setupPreferences(options = {}) {
    const setup = await readLocalState('/api/setup', options);
    if (!setup || setup.application !== 'plamo-for-cp' || options.signal?.aborted) return null;
    initializeGlossary(setup.default_glossary);
    const pending = GM_getValue('cp-translator-pending-model', null);
    if (pending && setup.preparation?.stage === 'ready' && setup.preparation.model === pending) { GM_setValue('cp-translator-model', pending); GM_deleteValue('cp-translator-pending-model'); }
    if (!GM_getValue('cp-translator-model', null) && MODELS.includes(setup.selected_model)) GM_setValue('cp-translator-model', setup.selected_model);
    if (Array.isArray(setup.models)) GM_setValue('cp-translator-model-weights', Object.fromEntries(setup.models.filter(model => Number.isFinite(model.weight_gb) && model.weight_gb > 0).map(model => [model.id, model.weight_gb.toFixed(2)])));
    GM_setValue('cp-translator-supported-batch-sizes', Array.isArray(setup.supported_batch_sizes) ? TRANSLATION_BATCH_SIZES.filter(size => setup.supported_batch_sizes.includes(size)) : [1]);
    GM_setValue('cp-translator-translation-jobs', setup.supported_translation_jobs === true);
    GM_setValue('cp-translator-translation-previews', setup.supported_translation_previews === true);
    return setup;
  }
  let connectionSetup;
  async function refreshConnection({ signal, refreshSetup = false } = {}) {
    const health = await readLocalState('/api/health', { signal });
    if (!health || (health.application || health.app) !== 'plamo-for-cp') {
      connectionSetup = null;
      return { connected: false, ready: false };
    }
    const userscript = { installed: USERSCRIPT_VERSION, available: health.userscript_version,
      update: compareUserscriptVersions(USERSCRIPT_VERSION, health.userscript_version) === -1 };
    if (refreshSetup || !connectionSetup) {
      const setup = await setupPreferences({ signal });
      if (!setup) return { connected: true, ready: false, userscript };
      connectionSetup = setup;
    }
    return { connected: true, ready: true, setup: connectionSetup, userscript };
  }
  let starting, started = false;
  function ensureCore() {
    if (started) return Promise.resolve();
    if (starting) return starting;
    const ready = document.documentElement ? Promise.resolve() : new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, { once: true }));
    starting = ready.then(() => {
      if (started) return; started = true;
      __TRANSLATOR_CORE__
    }).finally(() => { starting = null; });
    return starting;
  }
  GM_registerMenuCommand('このページを翻訳', () => ensureCore().then(() => commands.get('PLaMo: ページ全体を翻訳')?.()));
  GM_registerMenuCommand('テキストを翻訳', openText);
  GM_registerMenuCommand('設定・管理', openManagement);
  __TRANSLATOR_BROWSER_SETTINGS__
  if (nativePage && location.pathname === '/text') { setupPreferences().then(publishGlossary); return; }
  if (location.origin === API && location.pathname === '/') {
    setupPreferences().then(setup => {
      if (!setup) return;
      const mount = () => {
        const target = document.querySelector('#browser-settings'); if (!target) return false;
        renderBrowserSettings(target);
        document.documentElement.dataset.cpTranslatorUserscript = 'ready';
        document.addEventListener('cp-translator-model-selected', event => { const model = event.detail?.model; if (MODELS.includes(model)) writePreference('cp-translator-model', model); });
        document.dispatchEvent(new Event('cp-translator-browser-settings-ready'));
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
      const disabled = GM_getValue('cp-translator-disabled-sites', []).includes(site.id);
      if (site.documentType === 'general' && matchingCustomSite()) return true;
      if (!disabled) return true;
      if (problemRoots(site).length) return false;
    }
    return isCustomSite();
  };
  if (configured()) ensureCore();
  GM_addValueChangeListener('cp-translator-custom-sites', () => { if (!started && configured()) ensureCore(); });
  if (builtin()?.id === 'repovive') {
    const resume = () => { if (!started && configured()) ensureCore(); };
    new MutationObserver(resume).observe(document, { childList: true, subtree: true, characterData: true });
    window.addEventListener('popstate', resume);
  }
})();

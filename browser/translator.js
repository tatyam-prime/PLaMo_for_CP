// Browser translation core; host callbacks are provided by the userscript.
(() => {
  'use strict';

  const API = 'http://127.0.0.1:8765';
  const MODEL_IDS = ['0.8b', '2b', '8b'];
  const MODEL_WEIGHT_GB = { '0.8b': '0.98', '2b': '2.41', '8b': '6.07' };
  const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'CAPTION', 'DD', 'DETAILS', 'DIV', 'DL', 'DT', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'SECTION', 'TABLE', 'TBODY', 'TD', 'TFOOT', 'TH', 'THEAD', 'TR', 'UL']);
  const OMIT = 'script:not([type^="math/tex"]), style, noscript, template, button, input, textarea, select, nav, pre, .sample-test, .sample, .sampledata, .sample-data, .sample-input, .sample-output, .input-output, .problem-sample, .task-constraints, .problem-statement .header, [data-cp-translator-ui], [contenteditable="true"]';
  // Italic/bold classes can mark prose; monospace input tokens remain opaque.
  const PROTECTED = 'code, kbd, samp, var, img, svg, canvas, video, audio, iframe, object, br, hr, math, mjx-container, .math, .katex, .katex-display, .MathJax, .MathJax_Display, .MathJax_SVG, .MathJax_SVG_Display, .MathJax_Preview, .MathJax_CHTML, .MathJax_CHTML_Display, script[type^="math/tex"], .tex-span, .tex-font-style-tt, [data-cp-translator-preserve], [translate="no"]';
  const MARKER = /\[\[PLM_\d+\]\]|<\/?plm-[A-Za-z0-9]+>/g;
  const ADAPTERS = SITE_REGISTRY;

  let panel;
  let adapter;
  let running = false;
  let generation = 0;
  const activeRequests = new Set();
  let activePipeline;
  let clearActivePreviews;
  let forgetActiveAuto;
  let records = [];
  let showingOriginal = false;
  let generalRoot;
  let manualPanelURL;
  let pendingObserver;
  const autoSeen = new WeakMap();
  const managedConnection = typeof refreshConnection === 'function';
  let connectionState = managedConnection ? 'checking' : 'connected';
  let connectionSetup;
  let userscriptUpdate;
  let connectionTimer, connectionTask, connectionController;
  let connectionEpoch = 0, connectionFailures = 0, lastConnectionProbe = -Infinity;
  let refreshSetupPending = false;
  let pageAway = false;

  function element(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    return node;
  }

  function request(path, body, options = {}) {
    return new Promise((resolve, reject) => {
      let handle;
      const clear = () => activeRequests.delete(handle);
      handle = requestHTTP({
        method: body ? 'POST' : 'GET', url: API + path,
        headers: { 'Content-Type': 'application/json', 'X-Plamo-Client': 'userscript' },
        data: body ? JSON.stringify(body) : undefined,
        timeout: path === '/api/translate' ? 600000 : 10000,
        keepalive: options.keepalive === true,
        onload(response) {
          clear();
          let value;
          try { value = JSON.parse(response.responseText); }
          catch { reject(new Error('サーバーから JSON 以外の応答がありました。')); return; }
          if (response.status < 200 || response.status >= 300) {
            const detail = typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value.error || value);
            const error = new Error(detail || `HTTP ${response.status}`);
            error.status = response.status;
            reject(error);
          } else resolve(value);
        },
        onerror() { clear(); const error = new Error('翻訳アプリに接続できません。PLaMo for 競プロ アプリを開いてから、もう一度押してください。'); error.connection = true; reject(error); },
        ontimeout() { clear(); reject(new Error('応答がタイムアウトしました。PLaMo for 競プロ アプリでモデルの準備状況を確認してください。')); },
        onabort() { clear(); reject(new Error('翻訳を停止しました。')); },
      });
      if (!options.keepalive) activeRequests.add(handle);
    });
  }

  function connectionVisible() {
    return !!panel && panel.host.isConnected && !panel.host.hidden && !pageAway && document.visibilityState !== 'hidden';
  }

  function pauseConnectionMonitoring() {
    clearTimeout(connectionTimer); connectionTimer = undefined;
    connectionEpoch++; connectionController?.abort();
    connectionController = connectionTask = undefined;
    lastConnectionProbe = -Infinity;
    if (managedConnection) connectionState = 'checking';
  }

  function scheduleConnectionProbe(delay) {
    clearTimeout(connectionTimer); connectionTimer = undefined;
    if (!managedConnection || !connectionVisible()) return;
    connectionTimer = setTimeout(() => { connectionTimer = undefined; probeConnection(); }, delay);
  }

  function resumeConnectionMonitoring(immediate = false) {
    if (!connectionVisible()) { pauseConnectionMonitoring(); return; }
    if (managedConnection && (immediate || !connectionTimer && !connectionTask)) scheduleConnectionProbe(immediate || connectionState !== 'connected' ? 0 : 30000);
  }

  function renderConnection() {
    if (!panel) return;
    const connected = connectionState === 'connected';
    panel.box.dataset.connection = connectionState;
    panel.connection.hidden = connected;
    panel.connection.textContent = connectionState === 'checking' ? '接続確認中' : 'アプリ未接続';
    panel.launch.hidden = connected || connectionState === 'checking';
    panel.reconnect.hidden = connected;
    panel.reconnect.disabled = connectionState === 'checking';
    panel.translate.hidden = !connected || running;
    panel.translate.disabled = !connected || running;
    panel.model.hidden = !connected;
    panel.model.disabled = !connected || running;
    panel.settings.hidden = !connected;
    panel.update.hidden = !connected || !userscriptUpdate?.update;
    panel.update.title = userscriptUpdate?.update ? `翻訳スクリプトを更新してください（現在 v${userscriptUpdate.installed} → v${userscriptUpdate.available}）。更新後はこのページを再読み込みしてください。` : '';
    panel.toggle.hidden = !running && !records.length;
    panel.toggle.disabled = !running && !records.length;
    applyPanelPosition();
  }

  function probeConnection({ user = false, refreshSetup = false } = {}) {
    if (!connectionVisible()) return Promise.resolve(false);
    if (refreshSetup) refreshSetupPending = true;
    if (connectionTask) return connectionTask;
    const elapsed = Date.now() - lastConnectionProbe;
    if (!user && elapsed < 1500) { scheduleConnectionProbe(1500 - elapsed); return Promise.resolve(connectionState === 'connected'); }
    lastConnectionProbe = Date.now();
    const selected = readPreference('cp-translator-model', '2b');
    const needsSetup = refreshSetupPending || connectionState !== 'connected'
      || connectionSetup?.models?.find(model => model.id === selected)?.available === false;
    if (connectionState !== 'connected') { connectionState = 'checking'; renderConnection(); }
    const epoch = ++connectionEpoch, controller = new AbortController();
    connectionController = controller;
    const task = Promise.resolve().then(async () => {
      if (managedConnection) return refreshConnection({ signal: controller.signal, refreshSetup: needsSetup });
      const health = await request('/api/health');
      return { connected: (health.application || health.app) === 'plamo-for-cp', ready: true };
    }).catch(() => ({ connected: false, ready: false })).then(result => {
      if (epoch !== connectionEpoch || controller.signal.aborted || !connectionVisible()) return false;
      connectionState = result?.connected && result.ready ? 'connected' : result?.connected ? 'checking' : 'disconnected';
      userscriptUpdate = result?.userscript;
      if (result?.setup) connectionSetup = result.setup;
      if (connectionState === 'disconnected' && running) { forgetActiveAuto?.(); stop(); }
      renderConnection();
      if (connectionState === 'connected') {
        refreshSetupPending = false; connectionFailures = 0; updateModelChoice(); scheduleConnectionProbe(30000); scheduleAutoTranslation();
      } else {
        const delays = [3000, 6000, 12000, 30000, 60000];
        scheduleConnectionProbe(delays[Math.min(connectionFailures++, delays.length - 1)]);
      }
      return connectionState === 'connected';
    }).finally(() => { if (connectionTask === task) { connectionTask = connectionController = undefined; } });
    connectionTask = task;
    return task;
  }

  function preservationError(message) {
    const error = new Error(message);
    error.name = 'PreservationError';
    return error;
  }

  function snapshot(nodes) {
    return JSON.stringify(nodes.map(node => node.nodeType === Node.ELEMENT_NODE ? node.outerHTML : [node.nodeType, node.nodeValue]));
  }

  function inlineMath(node) {
    if (node.matches('script,img,svg,canvas,video,audio,iframe,object')) return false;
    const displayMath = '.katex-display,.math-display,.MathJax_Display,.MathJax_SVG_Display,.MathJax_CHTML_Display,mjx-container[display="true"],math[display="block"]';
    if (node.matches(displayMath) || node.querySelector(displayMath)) return false;
    if (!node.matches('var,code,math,mjx-container,.math,.katex,.MathJax,.MathJax_SVG,.MathJax_Preview,.MathJax_CHTML,.tex-span,.tex-font-style-it,.tex-font-style-bf')) return false;
    const style = getComputedStyle(node);
    return /^(?:inline(?:-.+)?|math|contents)$/.test(style.display) && style.visibility !== 'hidden';
  }

  // Formatting and invisible renderer bookkeeping do not separate visible
  // content. Keep independent inline slots distinct from prose and barriers.
  function boundaryItem(text, position, before, lookup, skipSpaces = false) {
    let remaining = before ? text.slice(0, position) : text.slice(position);
    let gap = false;
    while (remaining) {
      if (skipSpaces) {
        const trimmed = before ? remaining.trimEnd() : remaining.trimStart();
        if (trimmed.length !== remaining.length) { gap = true; remaining = trimmed; }
        if (!remaining) break;
      }
      const match = (before ? /(\[\[PLM_\d+\]\]|<\/?plm-[A-Za-z0-9]+>)$/ : /^(\[\[PLM_\d+\]\]|<\/?plm-[A-Za-z0-9]+>)/).exec(remaining);
      if (!match || (before && match.index + match[0].length !== remaining.length)) break;
      const marker = lookup.get(match[0]);
      if (!marker) return { kind: 'barrier', gap };
      if (!['open', 'close'].includes(marker.kind) && !marker.invisible) return marker.inlineMath ? { kind: 'math', marker, gap } : { kind: 'barrier', gap };
      remaining = before ? remaining.slice(0, match.index) : remaining.slice(match[0].length);
    }
    const characters = Array.from(remaining);
    const character = (before ? characters.at(-1) : characters[0]) || '';
    return { kind: /\s/u.test(character) ? 'space' : 'text', character, gap };
  }

  function restoredSlot(marker, text, position, lookup) {
    const node = marker.kind === 'text' ? document.createTextNode(marker.text) : marker.node.cloneNode(true);
    if (!marker.inlineMath) return node;
    const before = boundaryItem(text, position, true, lookup);
    const after = boundaryItem(text, position + marker.token.length, false, lookup);
    // The right-hand slot owns a missing math-to-math separator, including
    // when only the left slot carried source whitespace after a reordering.
    let start = before.kind === 'text' && marker.spaceBefore && /[\p{L}\p{N}]/u.test(before.character);
    if (before.kind === 'math') {
      const adjacent = marker.adjacentMath?.find(item => item.token === before.marker.token);
      start = adjacent ? adjacent.gap : marker.spaceBefore || before.marker.spaceAfter;
    }
    const end = after.kind === 'text' && marker.spaceAfter && /[\p{L}\p{N}]/u.test(after.character);
    if (!start && !end) return node;
    const fragment = document.createDocumentFragment();
    if (start) fragment.append(document.createTextNode(' '));
    fragment.append(node);
    if (end) fragment.append(document.createTextNode(' '));
    return fragment;
  }

  // Preserve DOM objects as markers. Paired markers retain formatting/link boundaries;
  // single markers retain the complete rendered math/code/image subtree.
  function serialize(nodes) {
    const markers = [];
    let protectedIndex = 0;
    let formattingIndex = 0;
    function alphabetic(index) {
      let result = '';
      do { result = String.fromCharCode(97 + index % 26) + result; index = Math.floor(index / 26) - 1; } while (index >= 0);
      return result;
    }
    function token(value, literal = `[[PLM_${protectedIndex++}]]`) {
      markers.push({ ...value, token: literal });
      return literal;
    }
    function inlineKaTeXWrapper(node) {
      if (node?.nodeType !== Node.ELEMENT_NODE || node.matches('.katex-display') || node.querySelector('.katex-display')) return false;
      const style = getComputedStyle(node);
      if (!/^(?:inline(?:-.+)?|math|contents)$/.test(style.display) || style.visibility === 'hidden') return false;
      if (node.matches('.katex')) return true;
      // TLX wraps each rendered expression (and empty renderer pieces) in an
      // otherwise empty span. Preserve that wrapper, without calling it prose
      // formatting that the model has to reproduce around every formula.
      if (node.tagName !== 'SPAN') return false;
      const contents = Array.from(node.childNodes).filter(child => child.nodeType !== Node.TEXT_NODE || child.data.trim());
      return contents.length === 1 && contents[0].nodeType === Node.ELEMENT_NODE && contents[0].matches('.katex') && inlineMath(contents[0]);
    }
    function walkNodes(children) {
      let result = '';
      const nextElement = start => { while (start < children.length && children[start].nodeType === Node.TEXT_NODE && !children[start].data.trim()) start++; return start; };
      for (let index = 0; index < children.length; index++) {
        const first = children[index];
        if (inlineKaTeXWrapper(first)) {
          let end = index;
          while (inlineKaTeXWrapper(children[nextElement(end + 1)])) end = nextElement(end + 1);
          const group = document.createDocumentFragment();
          for (const child of children.slice(index, end + 1)) group.append(child.cloneNode(true));
          result += token({ kind: 'protected', node: group, inlineMath: true, invisible: false });
          index = end; continue;
        }
        const preview = first.nodeType === Node.ELEMENT_NODE && first.matches('.MathJax_Preview');
        const rendererIndex = preview ? nextElement(index + 1) : index;
        const renderer = children[rendererIndex];
        if (renderer?.nodeType === Node.ELEMENT_NODE && renderer.matches('.MathJax,.MathJax_SVG,.MathJax_CHTML,.MathJax_Display,.MathJax_SVG_Display,.MathJax_CHTML_Display')) {
          const scriptIndex = nextElement(rendererIndex + 1), script = children[scriptIndex];
          const frame = renderer.id?.endsWith('-Frame') ? renderer : renderer.querySelector('[id$="-Frame"]');
          if (script?.nodeType === Node.ELEMENT_NODE && script.matches('script[type^="math/tex"]') && /^MathJax-Element-\d+$/.test(script.id) && frame?.id === script.id + '-Frame') {
            const group = document.createDocumentFragment();
            for (const child of children.slice(index, scriptIndex + 1)) group.append(child.cloneNode(true));
            result += token({ kind: 'protected', node: group, inlineMath: inlineMath(renderer), invisible: false });
            index = scriptIndex; continue;
          }
        }
        result += walk(first);
      }
      return result;
    }
    function walk(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        // Raw TeX can still be present when MathJax has not rendered yet.
        return node.data.replace(/\${6}[\s\S]*?\${6}|\$\$\$[\s\S]*?\$\$\$|\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|(?<!\\)\$(?!\s)[^$\n]+?\$/g, text => token({ kind: 'text', text, inlineMath: !text.startsWith('$$$$$$') && ((text.startsWith('$$$') && !text.includes('\n')) || (!text.startsWith('$$') && !text.startsWith('\\['))) }));
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return '';
      if (node.matches(PROTECTED)) {
        const style = getComputedStyle(node);
        return token({ kind: 'protected', node, inlineMath: inlineMath(node), invisible: style.display === 'none' || style.visibility === 'hidden' });
      }
      if (node.matches(OMIT)) return '';
      // HTML-like alphabetic tags convey formatting without introducing ordinal
      // numbers into the model's interpretation of the sentence.
      const tag = `plm-${alphabetic(formattingIndex++)}`;
      const open = token({ kind: 'open', node }, `<${tag}>`);
      const content = walkNodes(Array.from(node.childNodes));
      const close = token({ kind: 'close', open }, `</${tag}>`);
      return open + content + close;
    }
    const source = walkNodes(nodes);
    const lookup = new Map(markers.map(marker => [marker.token, marker]));
    for (const marker of markers) if (marker.inlineMath) {
      const position = source.indexOf(marker.token);
      marker.spaceBefore = boundaryItem(source, position, true, lookup).kind === 'space';
      marker.spaceAfter = boundaryItem(source, position + marker.token.length, false, lookup).kind === 'space';
      // Keep an original adjacent pair's separation even if the model reorders
      // it. Outer prose gaps must not separate an originally joined pair.
      marker.adjacentMath = [boundaryItem(source, position, true, lookup, true), boundaryItem(source, position + marker.token.length, false, lookup, true)]
        .filter(item => item.kind === 'math').map(item => ({ token: item.marker.token, gap: item.gap }));
    }
    return { source, markers };
  }

  function reconstruct(text, markers, partial = false) {
    if (partial) {
      const start = Math.max(text.lastIndexOf('[['), text.lastIndexOf('<'));
      if (start >= 0) {
        const tail = text.slice(start);
        if ((tail.startsWith('[[') && !tail.includes(']]')) || (tail.startsWith('<') && !tail.includes('>') && markers.some(marker => marker.token.startsWith(tail)))) text = text.slice(0, start);
      }
    }
    const root = document.createDocumentFragment();
    const stack = [{ node: root, open: null }];
    const lookup = new Map(markers.map(marker => [marker.token, marker]));
    const seen = new Set();
    let position = 0;
    for (const match of text.matchAll(MARKER)) {
      stack.at(-1).node.append(document.createTextNode(text.slice(position, match.index)));
      const literal = match[0];
      const marker = lookup.get(literal);
      if (!marker || seen.has(literal)) throw preservationError('数式・書式の保護マーカーが変わりました。原文を保持しています。');
      seen.add(literal);
      if (marker.kind === 'open') {
        const node = marker.node.cloneNode(false);
        stack.at(-1).node.append(node);
        stack.push({ node, open: literal });
      } else if (marker.kind === 'close') {
        if (stack.length === 1 || stack.at(-1).open !== marker.open) throw preservationError('翻訳で書式の対応が崩れました。原文を保持しています。');
        stack.pop();
      } else {
        stack.at(-1).node.append(restoredSlot(marker, text, match.index, lookup));
      }
      position = match.index + match[0].length;
    }
    if (!partial && (seen.size !== markers.length || stack.length !== 1)) throw preservationError('数式・書式の保護マーカーが欠けています。原文を保持しています。');
    stack.at(-1).node.append(document.createTextNode(text.slice(position)));
    return root;
  }

  function documentSourceLanguage() {
    const problemSelection = generalRoot?.isConnected && problemRoots(adapter).some(root => root === generalRoot || root.contains(generalRoot));
    const declared = !generalRoot?.isConnected || problemSelection ? adapter?.sourceLanguage : undefined;
    const language = (generalRoot?.closest('[lang]')?.getAttribute('lang') || document.documentElement.lang || '').toLowerCase();
    if (declared && declared !== 'auto') return declared;
    if (/^zh(?:-|$)/.test(language)) return 'Chinese';
    if (/^id(?:-|$)/.test(language)) return 'Indonesian';
    return declared || 'English';
  }

  function collect(roots, sourceLanguage = documentSourceLanguage()) {
    const result = [];
    const displayMath = '.katex-display,.MathJax_Display,.MathJax_SVG_Display,.MathJax_CHTML_Display,mjx-container[display="true"],math[display="block"]';
    const mathRoot = 'math,mjx-container,.katex,.katex-display,.MathJax,.MathJax_Display,.MathJax_SVG,.MathJax_SVG_Display,.MathJax_Preview,.MathJax_CHTML,.MathJax_CHTML_Display,script[type^="math/tex"]';
    function displayMathOnly(node) {
      if (node?.nodeType !== Node.ELEMENT_NODE || node.matches(OMIT) || node.querySelector(OMIT + ',a,iframe,object,video,audio,img,canvas,[contenteditable]')) return false;
      if (!node.matches(displayMath) && !node.querySelector(displayMath)) return false;
      function onlyMath(child) {
        if (child.nodeType === Node.TEXT_NODE) return !child.data.trim();
        if (child.nodeType === Node.COMMENT_NODE) return true;
        if (child.nodeType !== Node.ELEMENT_NODE) return false;
        if (child.matches(mathRoot)) return true;
        return ['SPAN', 'DIV'].includes(child.tagName) && child.childNodes.length > 0 && Array.from(child.childNodes).every(onlyMath);
      }
      return onlyMath(node);
    }
    function continuationEnd(children, start) {
      if (adapter?.id !== 'kattis' || generalRoot?.isConnected) return -1;
      const paragraph = node => node?.nodeType === Node.ELEMENT_NODE && node.tagName === 'P' && !node.matches(OMIT) && !node.querySelector(OMIT + ',iframe,object,video,audio,img,canvas');
      if (!paragraph(children[start])) return -1;
      const before = serialize(Array.from(children[start].childNodes)).source.replace(MARKER, '').trim();
      if (!/[A-Za-z]{2}/.test(before) || /[.!?。！？…]["'”’\])]*$/u.test(before)) return -1;
      const next = position => { while (position < children.length && (children[position].nodeType === Node.COMMENT_NODE || children[position].nodeType === Node.TEXT_NODE && !children[position].data.trim())) position++; return position; };
      let position = next(start + 1), count = 0;
      while (count < 8 && displayMathOnly(children[position])) { count++; position = next(position + 1); }
      if (!count || !paragraph(children[position])) return -1;
      const after = serialize(Array.from(children[position].childNodes)).source.replace(MARKER, '').trimStart();
      if (!/^(?:\p{Ll}|[,;:)\]])/u.test(after)) return -1;
      const limit = readPreference('cp-translator-translation-previews', false) === true ? 600 : 300;
      if (proseLength(serialize(children.slice(start, position + 1)).source) > limit) return -1;
      return position;
    }
    function visit(parent) {
      let run = [];
      function flush() {
        if (!run.length) return;
        const original = run;
        run = [];
        // An inline wrapper can contain a button/input/framework widget. Leave
        // its complete surrounding run untouched so listeners and state survive.
        if (original.some(node => node.nodeType === Node.ELEMENT_NODE && (node.matches(OMIT) || node.querySelector(OMIT)))) return;
        const { source, markers } = serialize(original);
        const words = source.replace(MARKER, '').trim();
        // Chinese adapters/documents also contain prose with no Latin letters.
        // Protected formula subtrees and numeric-only runs remain excluded.
        if (/[A-Za-z]{2}/.test(words) || sourceLanguage === 'Chinese' && /\p{Script=Han}/u.test(words)) result.push({ parent, original, source, markers, snapshot: snapshot(original) });
      }
      const children = Array.from(parent.childNodes);
      for (let index = 0; index < children.length; index++) {
        const child = children[index], end = continuationEnd(children, index);
        if (end >= 0) {
          flush(); run = children.slice(index, end + 1); flush(); index = end; continue;
        }
        if (child.nodeType === Node.ELEMENT_NODE && child.matches(OMIT)) { flush(); continue; }
        if (child.nodeType === Node.ELEMENT_NODE && BLOCK.has(child.tagName) && !child.matches(PROTECTED)) {
          flush(); visit(child);
        } else run.push(child);
      }
      flush();
    }
    for (const root of roots) visit(root);
    return result;
  }

  function proseLength(text) { return text.replace(MARKER, '').replace(/\s+/g, ' ').trim().length; }

  function markersIn(text, markers) {
    const tokens = new Set(text.match(MARKER) || []);
    return markers.filter(marker => tokens.has(marker.token));
  }

  function sentenceParts(source) {
    const sentences = [];
    let start = 0, depth = 0, last = '';
    for (const match of source.matchAll(/\[\[PLM_\d+\]\]|<\/?plm-[A-Za-z0-9]+>|\s+|./gsu)) {
      const token = match[0];
      if (token.startsWith('</plm-')) { depth--; continue; }
      if (token.startsWith('<plm-')) { depth++; continue; }
      if (token.startsWith('[[PLM_')) { last = ''; continue; }
      if (/^\s+$/u.test(token)) {
        const preceding = source.slice(Math.max(start, match.index - 80), match.index).replace(MARKER, '');
        const abbreviation = /(?:\b(?:e\.g|i\.e|Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)|\b[A-Z])\.$/i.test(preceding);
        if (!depth && /[.!?。！？]/u.test(last) && !abbreviation) { sentences.push(source.slice(start, match.index + token.length)); start = match.index + token.length; }
      } else last = token;
    }
    if (start < source.length) sentences.push(source.slice(start));
    return sentences;
  }

  function paragraphChunks(record) {
    const source = record.source;
    const targetLength = readPreference('cp-translator-translation-previews', false) === true ? 600 : 300;
    let parts = [source];
    if (proseLength(source) > targetLength) {
      parts = []; let current = '';
      for (const sentence of sentenceParts(source)) {
        if (current && proseLength(current + sentence) > targetLength) { parts.push(current); current = ''; }
        current += sentence;
      }
      if (current) parts.push(current);
    }
    record.chunks = parts.map(source => ({ parent: record, source, markers: markersIn(source, record.markers) }));
    return record.chunks;
  }

  function renderedChunk(chunk) {
    if (chunk.output === undefined) return chunk.source;
    const before = chunk.source.match(/^\s*/u)[0], after = chunk.source.match(/\s*$/u)[0];
    return (before && !/^\s/u.test(chunk.output) ? before : '') + chunk.output + (after && !/\s$/u.test(chunk.output) ? after : '');
  }

  function currentRecordUnchanged(record, originalView = showingOriginal) {
    if (!record.parent.isConnected || snapshot(record.original) !== record.snapshot) return false;
    if (!record.start) return record.original.every((node, index) => node.isConnected && node.parentNode === record.parent && (index === record.original.length - 1 || node.nextSibling === record.original[index + 1]));
    if (!record.start.isConnected || !record.end.isConnected || record.start.parentNode !== record.parent || record.end.parentNode !== record.parent) return false;
    const current = []; let node = record.start.nextSibling;
    while (node && node !== record.end) { current.push(node); node = node.nextSibling; }
    const expected = originalView ? record.original : record.translated;
    return node === record.end && current.length === expected.length && current.every((node, index) => node === expected[index])
      && (originalView || snapshot(current) === record.translatedSnapshot);
  }

  function applyFragment(record, fragment) {
    if (!currentRecordUnchanged(record)) throw new Error('ページの内容が更新されました。再読み込みして翻訳してください。');
    if (!record.start) {
      record.start = document.createComment('cp-translator-start'); record.end = document.createComment('cp-translator-end');
      record.parent.insertBefore(record.start, record.original[0]); record.original.at(-1).after(record.end);
    }
    if (!showingOriginal) {
      let node = record.start.nextSibling;
      while (node && node !== record.end) { const next = node.nextSibling; node.remove(); node = next; }
    }
    record.translated = Array.from(fragment.childNodes);
    record.translatedSnapshot = snapshot(record.translated);
    if (!showingOriginal) record.end.before(fragment);
    if (!records.includes(record)) records.push(record);
  }

  function renderRecord(record) {
    const fragment = document.createDocumentFragment();
    for (const chunk of record.chunks) {
      if (chunk.output === undefined && chunk.preview !== undefined) {
        const draft = element('span', undefined, { 'data-cp-translator-preview': '', title: '翻訳中の下書き' });
        draft.style.opacity = '0.75'; draft.append(reconstruct(chunk.preview, chunk.markers, true), document.createTextNode('…'));
        fragment.append(draft);
      } else fragment.append(reconstruct(renderedChunk(chunk), chunk.markers));
    }
    applyFragment(record, fragment);
  }

  function verifiedCount() { return records.filter(record => record.chunks.some(chunk => chunk.output !== undefined)).length; }

  function unwrapPreviewDecoration(record) {
    for (const node of record.translated || []) if (node.nodeType === Node.ELEMENT_NODE && node.hasAttribute('data-cp-translator-preview') && node.isConnected) {
      if (node.lastChild?.nodeType === Node.TEXT_NODE && node.lastChild.data === '…') node.lastChild.remove();
      node.replaceWith(...node.childNodes);
    }
  }

  function refreshOrRestore(record) {
    if (record.chunks.some(chunk => chunk.output !== undefined || chunk.preview !== undefined)) { renderRecord(record); return; }
    if (!record.start) return;
    const unchanged = currentRecordUnchanged(record);
    if (unchanged && !showingOriginal) {
      let node = record.start.nextSibling;
      while (node && node !== record.end) { const next = node.nextSibling; node.remove(); node = next; }
      for (const original of record.original) record.end.before(original);
    } else if (!unchanged) unwrapPreviewDecoration(record);
    record.start.remove(); record.end.remove(); record.start = record.end = undefined;
    const index = records.indexOf(record); if (index >= 0) records.splice(index, 1);
  }

  function toggle() {
    const previousView = showingOriginal;
    showingOriginal = !showingOriginal;
    for (const record of records) {
      if (!currentRecordUnchanged(record, previousView)) continue;
      let current = record.start.nextSibling;
      while (current && current !== record.end) { const next = current.nextSibling; current.remove(); current = next; }
      for (const node of showingOriginal ? record.original : record.translated) record.end.before(node);
    }
    buttonLabel(panel.toggle, showingOriginal ? '訳文を表示' : '原文を表示');
    panel.toggle.setAttribute('aria-pressed', String(showingOriginal));
    panel.toggle.classList.toggle('active', showingOriginal);
  }

  function restore() {
    if (!showingOriginal && records.length) toggle();
    for (const record of records) { record.start?.remove(); record.end?.remove(); }
    records = [];
    showingOriginal = false;
    buttonLabel(panel.toggle, '原文を表示');
    panel.toggle.setAttribute('aria-pressed', 'false');
    panel.toggle.disabled = true;
    panel.toggle.hidden = true;
    panel.toggle.classList.remove('active');
  }

  function rootsForPage() {
    if (generalRoot?.isConnected) return [generalRoot];
    return problemRoots(adapter);
  }

  function status(text, error = false) {
    panel.status.textContent = text;
    panel.box.title = text + ' · Built with PLaMo';
    panel.notice.hidden = !error;
    panel.notice.title = text;
    panel.settings.title = error ? '設定・管理 — 翻訳の準備を確認' : '設定・管理';
  }

  async function translate() {
    if (running) return;
    const requestedURL = location.href;
    if (managedConnection && !await probeConnection()) {
      status('アプリに接続できません。「起動」から PLaMo for 競プロ を開き、再接続してください。', true);
      return;
    }
    if (running || location.href !== requestedURL) return;
    const roots = rootsForPage();
    if (!roots.length) { status('問題文が見つかりません。表示完了後に再試行してください。', true); return; }
    const documentType = generalRoot?.isConnected || adapter?.documentType === 'general' ? 'general' : 'problem';
    const sourceLanguage = documentSourceLanguage();
    restore();
    const work = collect(roots, sourceLanguage).flatMap(paragraphChunks);
    if (!work.length) { status('翻訳する文章が見つかりません。'); return; }
    const allUnits = new Set(work);
    const runURL = location.href;
    for (const root of roots) autoSeen.set(root, { url: runURL, text: root.textContent });
    const saved = readPreference('cp-translator-model', '2b');
    const model = MODEL_IDS.includes(saved) ? saved : '2b';
    panel.model.value = model;
    const savedBatch = readPreference('cp-translator-batch-size', 16);
    const requestedBatch = Number.isInteger(savedBatch) && savedBatch >= 1 && savedBatch <= 32 ? savedBatch : 16;
    const supportedBatches = readPreference('cp-translator-supported-batch-sizes', [1]);
    const batchSize = Array.isArray(supportedBatches) && supportedBatches.includes(requestedBatch) ? requestedBatch : 1;
    const configuredGlossary = readPreference('cp-translator-glossary', null);
    const defaults = readPreference('cp-translator-default-glossary', []);
    const glossary = documentType === 'general'
      ? (Array.isArray(configuredGlossary) ? configuredGlossary.filter(pair => !Array.isArray(defaults) || !defaults.some(item => item.source.toLowerCase() === pair.source.toLowerCase() && item.target === pair.target)) : [])
      : configuredGlossary;
    running = true;
    const thisGeneration = ++generation;
    panel.translate.disabled = true;
    panel.translate.hidden = true;
    panel.model.disabled = true;
    panel.stop.disabled = false;
    panel.stop.hidden = false;
    panel.toggle.disabled = false;
    panel.toggle.hidden = false;
    panel.progress.max = work.length;
    panel.progress.value = 0;
    panel.progress.hidden = false;
    panel.count.hidden = false;
    panel.count.textContent = `0/${work.length}`;
    const failedWork = new Set();
    let lastFailure = '';
    let rememberAuto = true;
    const forgetAuto = () => { rememberAuto = false; };
    forgetActiveAuto = forgetAuto;
    const consume = (unit, text, message) => {
      try {
        if (message) throw preservationError(message);
        if (typeof text !== 'string' || !text.trim()) throw preservationError('翻訳結果が空です。原文を保持しています。');
        reconstruct(text, unit.markers);
        const previous = unit.output;
        unit.output = text;
        try { renderRecord(unit.parent); }
        catch (error) { unit.output = previous; throw error; }
        failedWork.delete(unit);
      } catch (error) {
        if (error.name !== 'PreservationError') throw error;
        failedWork.add(unit); lastFailure = error.message;
        const level = unit.retryLevel || 0;
        if (level < 2) {
          unit.retryLevel = 2;
          const parts = sentenceParts(unit.source);
          if (parts.length >= 2 && parts.length <= 32 && parts.join('') === unit.source && parts.every(part => part.trim())) {
            let sources = parts, childLevel = 2;
            if (!level && parts.length >= 3) {
              const minimum = parts.length >= 4 ? 2 : 1;
              let cut = minimum, difference = Infinity;
              for (let index = minimum; index <= parts.length - minimum; index++) {
                const distance = Math.abs(proseLength(parts.slice(0, index).join('')) - proseLength(parts.slice(index).join('')));
                if (distance < difference) { cut = index; difference = distance; }
              }
              sources = [parts.slice(0, cut).join(''), parts.slice(cut).join('')]; childLevel = 1;
            }
            if (!currentRecordUnchanged(unit.parent)) throw new Error('ページの内容が更新されました。再読み込みして翻訳してください。');
            const index = unit.parent.chunks.indexOf(unit);
            if (index >= 0) {
              const children = sources.map(source => ({ parent: unit.parent, source, markers: markersIn(source, unit.markers), retryLevel: childLevel }));
              unit.parent.chunks.splice(index, 1, ...children);
              failedWork.delete(unit); allUnits.delete(unit);
              for (const child of children) { allUnits.add(child); failedWork.add(child); }
              return children;
            }
          }
        }
      }
      return [];
    };
    const clearPreviews = () => {
      const parents = new Set();
      for (const unit of allUnits) if (unit.preview !== undefined) { delete unit.preview; parents.add(unit.parent); }
      for (const parent of parents) { try { refreshOrRestore(parent); } catch { unwrapPreviewDecoration(parent); } }
    };
    clearActivePreviews = clearPreviews;
    const fields = { model, precision: '4bit', source_lang: sourceLanguage, target_lang: 'Japanese', document_type: documentType, glossary: Array.isArray(glossary) ? glossary : null };
    const runPhase = async (items, receive) => {
      panel.progress.max = items.length; panel.progress.value = 0;
      let retrying = false;
      const ids = new Map(items.map((item, index) => [item, String(index)]));
      const append = children => children.map(child => {
        const id = String(items.length); items.push(child); ids.set(child, id); retrying = true;
        return { id, text: child.source };
      });
      const ensureAllowed = () => {
        if (thisGeneration !== generation) return false;
        if (location.href !== runURL) throw new Error('ページの内容が更新されました。再読み込みして翻訳してください。');
        return true;
      };
      const progress = (completed, total) => {
        if (thisGeneration !== generation) return;
        panel.progress.max = total;
        panel.progress.value = completed; panel.count.textContent = completed + '/' + total;
        status((retrying ? '文を分けて再試行中… ' : '') + completed + ' / ' + total + ' を翻訳中…');
      };
      if (!ensureAllowed()) return;
      if (readPreference('cp-translator-translation-jobs', false) === true) {
        const pipeline = startTranslationPipeline({
          segments: items.map((item, index) => ({ id: String(index), text: item.source })), batchSize, fields,
          request,
          onPreview(preview) {
            const unit = items[Number(preview.id)];
            if (!ensureAllowed()) return;
            if (!unit || unit.output !== undefined) return;
            try { reconstruct(preview.text, unit.markers, true); unit.preview = preview.text; renderRecord(unit.parent); }
            catch (error) {
              if (error.name !== 'PreservationError') throw error;
              delete unit.preview; refreshOrRestore(unit.parent);
            }
          },
          onResult(event) {
            if (!ensureAllowed()) return;
            const item = items[Number(event.id)], hadPreview = item.preview !== undefined;
            delete item.preview;
            let children;
            try {
              children = receive(item, event.text, event.type === 'segment_error' ? event.message || '翻訳できなかった段落は原文を保持しています。' : '');
            } finally {
              if (hadPreview) {
                try { refreshOrRestore(item.parent); }
                catch { unwrapPreviewDecoration(item.parent); }
              }
            }
            if (children.length && ensureAllowed()) pipeline.enqueue(append(children), { priority: true });
          },
          onProgress({ completed, total }) { progress(completed, total); },
        });
        activePipeline = pipeline;
        try { await pipeline.promise; } finally { if (activePipeline === pipeline) activePipeline = undefined; }
      } else {
        const pending = items.slice(); let completed = 0;
        while (pending.length) {
          if (!ensureAllowed()) return;
          const group = pending.splice(0, batchSize), retries = [];
          const receiveLegacy = (item, text, error) => { const children = receive(item, text, error); append(children); retries.push(...children); };
          progress(completed, items.length);
          try {
            const response = await request('/api/translate', { ...fields, source_context: '', batch_size: batchSize,
              segments: group.map(item => ({ id: ids.get(item), text: item.source })) });
            if (!ensureAllowed()) return;
            for (let offset = 0; offset < group.length; offset++) {
              const id = ids.get(group[offset]), error = response.errors?.find(item => item.id === id);
              const outputs = Array.isArray(response.translations) ? response.translations.filter(item => item.id === id) : [];
              receiveLegacy(group[offset], outputs[0]?.text, error ? error.message || '翻訳できなかった段落は原文を保持しています。' : outputs.length !== 1 ? '翻訳結果が空または重複しています。原文を保持しています。' : '');
            }
          } catch (error) {
            if (thisGeneration !== generation) return;
            if (error.name !== 'PreservationError' && error.status !== 422) throw error;
            for (const item of group) receiveLegacy(item, '', error.message);
          }
          pending.unshift(...retries);
          completed += group.length; progress(completed, items.length);
        }
      }
    };
    try {
      status('ローカルサーバーに接続しています…');
      if (!managedConnection) {
        const health = await request('/api/health');
        if ((health.application || health.app) !== 'plamo-for-cp') { const error = new Error('翻訳アプリを確認できません。PLaMo for 競プロ を開いてください。'); error.connection = true; throw error; }
      }
      if (thisGeneration !== generation) return;
      await runPhase(work, consume);
      if (thisGeneration !== generation) return;
      const failed = failedWork.size;
      status(`${verifiedCount()} 箇所を翻訳しました。${failed ? `${failed} 箇所は失敗したため原文を保持しました。${lastFailure}` : '数式・サンプルは原文のままです。'}`, failed > 0);
    } catch (error) {
      if (error.connection || error.status === 409) {
        rememberAuto = false;
        if (error.connection) { connectionState = 'disconnected'; renderConnection(); scheduleConnectionProbe(3000); }
        else {
          const models = connectionSetup?.models || [];
          connectionSetup = { ...connectionSetup, models: [...models.filter(item => item.id !== model), { id: model, available: false }] };
        }
      }
      if (thisGeneration === generation) status(`${error.message}${verifiedCount() ? `（${verifiedCount()} 箇所は翻訳済み）` : ''}`, true);
    } finally {
      clearPreviews(); if (clearActivePreviews === clearPreviews) clearActivePreviews = undefined;
      if (forgetActiveAuto === forgetAuto) forgetActiveAuto = undefined;
      if (location.href === runURL) for (const root of roots) {
        if (rememberAuto) autoSeen.set(root, { url: runURL, text: root.textContent });
        else autoSeen.delete(root);
      }
      if (thisGeneration === generation) finish();
      scheduleAutoTranslation();
    }
  }

  function finish() {
    running = false;
    panel.translate.disabled = false;
    panel.translate.hidden = false;
    panel.model.disabled = false;
    panel.stop.disabled = true;
    panel.stop.hidden = true;
    panel.toggle.disabled = !records.length;
    panel.toggle.hidden = !records.length;
    panel.progress.hidden = true;
    panel.count.hidden = true;
    updateModelChoice();
  }

  function stop() {
    generation++;
    activePipeline?.cancel(); activePipeline = undefined;
    for (const handle of [...activeRequests]) handle.abort();
    activeRequests.clear();
    clearActivePreviews?.(); clearActivePreviews = undefined;
    finish();
    status(`停止しました。${verifiedCount()} 箇所を翻訳済みです。サーバー側の実行中の推論は完了まで続く場合があります。`);
  }

  function buttonLabel(button, label) {
    button.setAttribute('aria-label', label);
    button.title = label;
  }

  // The page can inspect the toolbar DOM, but cannot use synthetic events to
  // invoke the userscript's local requests or write its cross-site preferences.
  const userAction = action => event => { if (event.isTrusted) action(event); };

  function iconButton(label, paths, className = '') {
    const button = element('button', undefined, { class: className });
    buttonLabel(button, label);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', width: '19', height: '19', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(name, value);
    for (const data of paths) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', data);
      svg.append(path);
    }
    button.append(svg);
    return button;
  }

  function applyPanelPosition() {
    if (!panel || panel.dragging) return;
    const offset = readPreference('cp-translator-panel-offset', null);
    if (offset && Number.isFinite(offset.x) && Number.isFinite(offset.y)) {
      const bounds = panel.box.getBoundingClientRect();
      const maxX = Math.max(8, innerWidth - bounds.width - 8), maxY = Math.max(8, innerHeight - bounds.height - 8);
      panel.host.style.left = `${8 + Math.max(0, Math.min(1, offset.x)) * (maxX - 8)}px`;
      panel.host.style.top = `${8 + Math.max(0, Math.min(1, offset.y)) * (maxY - 8)}px`;
      panel.host.style.right = panel.host.style.bottom = 'auto';
      return;
    }
    const position = readPreference('cp-translator-panel-position', 'right-bottom');
    panel.host.style.left = position.startsWith('left-') ? '14px' : 'auto';
    panel.host.style.right = position.startsWith('left-') ? 'auto' : '14px';
    panel.host.style.top = position.endsWith('-top') ? '14px' : 'auto';
    panel.host.style.bottom = position.endsWith('-top') ? 'auto' : '14px';
  }

  function enableDragging(grip) {
    function moveTo(x, y, save) {
      const bounds = panel.box.getBoundingClientRect();
      const maxX = Math.max(8, innerWidth - bounds.width - 8), maxY = Math.max(8, innerHeight - bounds.height - 8);
      const left = Math.max(8, Math.min(maxX, x)), top = Math.max(8, Math.min(maxY, y));
      panel.host.style.left = `${left}px`; panel.host.style.top = `${top}px`;
      panel.host.style.right = panel.host.style.bottom = 'auto';
      if (save) writePreference('cp-translator-panel-offset', { x: maxX > 8 ? (left - 8) / (maxX - 8) : 0, y: maxY > 8 ? (top - 8) / (maxY - 8) : 0 });
    }
    let start;
    grip.addEventListener('pointerdown', event => {
      if (!event.isTrusted || event.button !== 0) return;
      const rect = panel.host.getBoundingClientRect();
      start = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
      panel.dragging = true; grip.setPointerCapture(event.pointerId); event.preventDefault();
    });
    grip.addEventListener('pointermove', event => { if (event.isTrusted && start) moveTo(start.left + event.clientX - start.x, start.top + event.clientY - start.y, false); });
    const finish = event => {
      if (!event.isTrusted || !start) return;
      const rect = panel.host.getBoundingClientRect(); start = undefined; panel.dragging = false;
      if (grip.hasPointerCapture(event.pointerId)) grip.releasePointerCapture(event.pointerId);
      moveTo(rect.left, rect.top, true);
    };
    grip.addEventListener('pointerup', finish); grip.addEventListener('pointercancel', finish);
    grip.addEventListener('keydown', event => {
      if (!event.isTrusted) return;
      if (event.key === 'Home') {
        event.preventDefault(); writePreference('cp-translator-panel-offset', null); writePreference('cp-translator-panel-position', 'right-bottom'); applyPanelPosition(); return;
      }
      const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      if (!direction) return;
      event.preventDefault(); const rect = panel.host.getBoundingClientRect(), step = event.shiftKey ? 40 : 10;
      moveTo(rect.left + direction[0] * step, rect.top + direction[1] * step, true);
    });
    window.addEventListener('resize', applyPanelPosition);
    new ResizeObserver(applyPanelPosition).observe(panel.box);
  }

  function updateModelChoice() {
    if (!panel) return;
    applyPanelPosition();
    const restriction = translationRestriction();
    panel.restrictionKey = restriction ? JSON.stringify([restriction, location.href]) : '';
    const suppress = restriction && manualPanelURL !== location.href;
    if (suppress) {
      panel.policyHidden = true;
      panel.host.hidden = true;
      if (running) stop();
    } else if (panel.policyHidden) {
      panel.policyHidden = false;
      panel.host.hidden = false;
    }
    panel.restriction.hidden = true;
    const restrictionMessage = restriction ? typeof restriction === 'string' ? restriction : restriction.message : '';
    panel.restriction.textContent = restrictionMessage;
    if (restriction?.url && /^https:\/\//.test(restriction.url)) panel.restriction.append(document.createTextNode(' '), element('a', '大会規定', { href: restriction.url, target: '_blank', rel: 'noopener noreferrer' }));
    panel.translate.disabled = running;
    panel.model.disabled = running;
    panel.toggle.disabled = !running && !records.length;
    renderConnection();
    resumeConnectionMonitoring();
    if (running) return;
    const saved = readPreference('cp-translator-model', '2b');
    panel.model.value = MODEL_IDS.includes(saved) ? saved : '2b';
    const weights = readPreference('cp-translator-model-weights', MODEL_WEIGHT_GB);
    panel.model.title = `PLaMo 3 Translate ${panel.model.value.toUpperCase()} · 約${weights[panel.model.value] || MODEL_WEIGHT_GB[panel.model.value]} GB`;
  }

  function openPanel(selectedRoot) {
    if (translationRestriction() && manualPanelURL !== location.href) { if (panel) updateModelChoice(); return false; }
    if (selectedRoot) generalRoot = selectedRoot;
    if (panel) { panel.host.hidden = false; updateModelChoice(); return; }
    const host = element('div', undefined, { 'data-cp-translator-ui': '', id: 'cp-translator' });
    const shadow = host.attachShadow({ mode: 'open' });
    const style = element('style');
    style.textContent = `:host{all:initial;position:fixed;bottom:14px;right:14px;z-index:2147483647;font:12px/1.4 system-ui,sans-serif;color:#34465b;color-scheme:light}:host([hidden]),[hidden]{display:none!important}*{box-sizing:border-box}.panel{position:relative;display:flex;align-items:center;gap:3px;width:max-content;max-width:calc(100vw - 20px);height:38px;padding:3px 4px;border:1px solid #d6dfe8;border-radius:9px;background:#fffffff5;box-shadow:0 2px 10px #17314b17}button{position:relative;display:inline-flex;align-items:center;justify-content:center;width:28px;height:30px;padding:4px;border:0;border-radius:6px;background:transparent;color:inherit;cursor:pointer}button:hover{background:#edf2f8}button:disabled{opacity:.45;cursor:default}button.primary{width:30px;background:#315fce;color:white}button.primary:hover{background:#244eae}button.active{background:#e7efff;color:#2454bb}button.close{width:24px;color:#8b98a6}select{width:55px;height:28px;min-width:0;padding:2px 1px;border:1px solid #d6dfe8;border-radius:5px;background:white;font:12px system-ui,sans-serif;color:#34465b}select:disabled{opacity:.5}.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.count{position:absolute;bottom:42px;right:0;border:1px solid #dbe3ec;border-radius:5px;background:white;padding:1px 5px;color:#6c7e91;font-size:10px;font-variant-numeric:tabular-nums}.progress{position:absolute;left:4px;bottom:0;width:calc(100% - 8px);height:3px;border:0;border-radius:3px;overflow:hidden;appearance:none;background:#dce8ff;accent-color:#1765f5}.progress::-webkit-progress-bar{background:#dce8ff;border-radius:3px}.progress::-webkit-progress-value{background:#1765f5;border-radius:3px}.progress::-moz-progress-bar{background:#1765f5;border-radius:3px}.notice{position:absolute;top:-2px;right:-1px;width:10px;height:10px;border-radius:50%;background:#d28b16;color:white;font:700 9px/10px system-ui;text-align:center}`;
    const box = element('section', undefined, { class: 'panel', 'aria-label': 'PLaMo 翻訳' });
    style.textContent += '.grip{position:absolute;inset-inline-start:4px;top:50%;transform:translateY(-50%);width:12px;padding:1px;touch-action:none;cursor:grab;color:#8b98a6}.grip:active{cursor:grabbing}.grip svg{width:10px}';
    const grip = iconButton('翻訳バーを移動', ['M8 5h.01M16 5h.01M8 12h.01M16 12h.01M8 19h.01M16 19h.01'], 'grip');
    grip.title = 'ドラッグで移動。矢印キーで移動、Homeキーで右下に戻す';
    const translateButton = iconButton('日本語に翻訳', ['M3 5h11M8 3v2M12 5c0 6-3 9-8 12M5 8c1 3 4 6 7 7M13 21l4.5-11L22 21M15 17h5'], 'primary');
    const stopButton = iconButton('停止', ['M7 7h10v10H7z'], 'primary');
    stopButton.hidden = true; stopButton.disabled = true;
    const toggleButton = iconButton('原文を表示', ['M2 12s3-6 10-6 10 6 10 6-3 6-10 6S2 12 2 12Z', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z']);
    toggleButton.hidden = true; toggleButton.disabled = true; toggleButton.setAttribute('aria-pressed', 'false');
    const settings = iconButton('設定・管理', ['M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z', 'M12 1v3M12 20v3M1 12h3M20 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1']);
    settings.addEventListener('click', userAction(() => openManagement()));
    const notice = element('span', '!', { class: 'notice', 'aria-hidden': 'true', hidden: '' });
    settings.append(notice);
    const update = element('a', '更新', { class: 'update', href: API + '/install.user.js', target: '_blank', rel: 'noopener noreferrer', 'aria-label': '翻訳スクリプトを更新', hidden: '' });
    update.addEventListener('click', event => {
      update.href = API + '/install.user.js';
      event.stopImmediatePropagation();
      if (!event.isTrusted) event.preventDefault();
    });
    const close = iconButton('パネルを閉じる', ['M6 6l12 12M18 6 6 18'], 'close');
    close.addEventListener('click', userAction(() => { if (running) stop(); host.hidden = true; pauseConnectionMonitoring(); }));
    const model = element('select', undefined, { 'aria-label': 'モデルサイズ' });
    for (const id of MODEL_IDS) model.append(element('option', id.toUpperCase(), { value: id }));
    model.addEventListener('change', event => { if (event.isTrusted && MODEL_IDS.includes(model.value)) writePreference('cp-translator-model', model.value); updateModelChoice(); });
    translateButton.addEventListener('click', userAction(translate));
    stopButton.addEventListener('click', userAction(stop));
    toggleButton.addEventListener('click', userAction(toggle));
    const count = element('span', '', { class: 'count', hidden: '', 'aria-hidden': 'true' });
    const progress = element('progress', undefined, { class: 'progress', max: '1', value: '0', hidden: '', 'aria-label': '翻訳の進捗' });
    const message = element('div', '問題文の英文を日本語に翻訳します。', { class: 'status sr-only', role: 'status', 'aria-live': 'polite' });
    const site = element('span', adapter?.name || '一般文書', { class: 'site sr-only' });
    const restriction = element('div', '', { class: 'restriction', role: 'note', hidden: '' });
    const controls = element('div', undefined, { class: 'controls' });
    const connection = element('span', '接続確認中', { class: 'connection', role: 'status', 'aria-live': 'polite', hidden: '' });
    const launch = element('a', '起動', { class: 'launch', href: 'plamo-for-cp://open', 'aria-label': 'アプリを起動', title: 'PLaMo for 競プロ を起動します。ブラウザーの確認が表示される場合があります。', hidden: '' });
    launch.addEventListener('click', event => {
      launch.href = 'plamo-for-cp://open';
      event.stopImmediatePropagation();
      if (!event.isTrusted) { event.preventDefault(); return; }
      scheduleConnectionProbe(750);
    });
    const reconnect = iconButton('再接続', ['M20 7v5h-5', 'M4 17v-5h5', 'M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17']);
    reconnect.hidden = true;
    reconnect.addEventListener('click', userAction(() => probeConnection({ user: true, refreshSetup: true })));
    controls.append(grip, translateButton, stopButton, model, connection, launch, reconnect, toggleButton, settings, update, close);
    const attribution = element('span', 'Translated by PLaMo 3 Translate', { class: 'attribution' });
    style.textContent += '.panel{flex-direction:column;gap:1px;height:auto;padding-inline-start:19px;padding-bottom:4px}.controls{display:flex;align-items:center;gap:3px}.attribution{font:10px/12px system-ui,sans-serif;color:#6c7e91;white-space:nowrap}.count{bottom:calc(100% + 4px)}.restriction{margin:3px 0 0;padding:5px;width:190px;max-width:calc(100vw - 40px);font:11px/1.5 system-ui,sans-serif;color:#80591f;background:#fff5dc;border-radius:5px}';
    style.textContent += '.connection{font:11px/30px system-ui,sans-serif;color:#6c7e91;white-space:nowrap}.launch{display:inline-flex;align-items:center;justify-content:center;height:26px;padding:0 6px;border-radius:5px;background:#315fce;color:white;font:11px system-ui,sans-serif;text-decoration:none}.launch:hover{background:#244eae}';
    style.textContent += '.update{display:inline-flex;align-items:center;justify-content:center;height:26px;padding:0 5px;border-radius:5px;background:#e7efff;color:#2454bb;font:11px system-ui,sans-serif;text-decoration:none;white-space:nowrap}.update:hover{background:#dbe7ff}';
    box.append(controls, attribution, restriction, count, progress, message, site);
    shadow.append(style, box);
    document.documentElement.append(host);
    panel = { host, box, model, translate: translateButton, stop: stopButton, toggle: toggleButton, settings, update, notice, status: message, progress, count, restriction, connection, launch, reconnect };
    enableDragging(grip);
    updateModelChoice();
    document.addEventListener('cp-translator-preferences-changed', updateModelChoice);
  }

  function openGeneral(mode = 'auto', explicit = true) {
    if (explicit) manualPanelURL = location.href;
    const selection = window.getSelection();
    let root;
    if (mode !== 'page' && selection?.rangeCount && !selection.isCollapsed) {
      const container = selection.getRangeAt(0).commonAncestorContainer;
      root = container.nodeType === Node.ELEMENT_NODE ? container : container.parentElement;
      root = root.closest('p, li, section, article, main, .markdown-body') || root;
    }
    if (!root && mode === 'selection') {
      window.alert('翻訳したい文章を選択してから、もう一度実行してください。');
      return false;
    }
    root ||= document.querySelector('article, main, [role="main"], .markdown-body') || (mode === 'page' ? document.body : undefined);
    if (!root || root.closest('[data-cp-translator-ui]')) {
      window.alert('翻訳したい本文を選択してから、もう一度メニューを実行してください。');
      return false;
    }
    openPanel(root);
    status('選択範囲を含む段落、または記事の本文を翻訳します。');
    return true;
  }

  function openPage() {
    manualPanelURL = location.href;
    if (problemRoots(adapter).length) {
      generalRoot = undefined;
      openPanel();
      return true;
    }
    return openGeneral('page');
  }

  let autoTimer;
  function automaticEnabled() {
    if (translationRestriction() || connectionState !== 'connected') return false;
    const selected = readPreference('cp-translator-model', '2b');
    if (connectionSetup?.models?.find(model => model.id === selected)?.available === false) return false;
    const site = readPreference('cp-translator-current-site', null);
    return typeof site === 'string' && readPreference('cp-translator-auto-sites', {})?.[site] === true;
  }
  function scheduleAutoTranslation() {
    clearTimeout(autoTimer);
    const restriction = translationRestriction();
    if (panel && panel.restrictionKey !== (restriction ? JSON.stringify([restriction, location.href]) : '')) updateModelChoice();
    if (restriction) return;
    if (!panel && adapter && !readPreference('cp-translator-disabled-sites', []).includes(adapter.id) && problemRoots(adapter).length) openPanel();
    if (!automaticEnabled()) return;
    autoTimer = setTimeout(() => {
      if (running || !automaticEnabled()) return;
      const disabled = readPreference('cp-translator-disabled-sites', []);
      const judgeAllowed = adapter && !disabled.includes(adapter.id);
      let roots = judgeAllowed ? problemRoots(adapter) : [];
      if (roots.length) generalRoot = undefined;
      else if (readPreference('cp-translator-custom-site', false)) {
        const root = document.querySelector('article, main, [role="main"], .markdown-body') || document.body;
        if (!root) return;
        roots = [root];
        if (generalRoot !== root) openPanel(root);
      } else return;
      if (roots.every(root => { const seen = autoSeen.get(root); return seen && (seen.url === location.href || seen.text === root.textContent); })) return;
      if (!collect(roots).length) return;
      openPanel();
      translate();
    }, 100);
  }

  const autoObserver = new MutationObserver(changes => {
    if (changes.some(change => !(change.target.nodeType === Node.ELEMENT_NODE ? change.target : change.target.parentElement)?.closest('[data-cp-translator-ui]'))) scheduleAutoTranslation();
  });
  autoObserver.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  window.addEventListener('pagehide', () => { pageAway = true; pauseConnectionMonitoring(); if (running) stop(); });
  window.addEventListener('pageshow', () => { pageAway = false; renderConnection(); resumeConnectionMonitoring(true); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') pauseConnectionMonitoring(); else { renderConnection(); resumeConnectionMonitoring(true); } });
  window.addEventListener('focus', event => { if (event.isTrusted) resumeConnectionMonitoring(true); });
  document.addEventListener('cp-translator-preferences-changed', () => {
    const selected = readPreference('cp-translator-model', '2b');
    if (connectionState === 'connected' && connectionSetup?.models?.find(model => model.id === selected)?.available === false) probeConnection({ refreshSetup: true });
    scheduleAutoTranslation();
  });
  window.addEventListener('popstate', scheduleAutoTranslation);

  registerCommand('PLaMo: このページの本文を翻訳', openGeneral);
  registerCommand('PLaMo: ページ全体を翻訳', async () => { if (openPage()) await translate(); });
  registerCommand('PLaMo: 選択範囲を翻訳', async () => { if (openGeneral('selection')) await translate(); });
  registerCommand('PLaMo: 翻訳パネルを表示', () => { if (panel) openPanel(); else openGeneral(); });
  adapter = ADAPTERS.find(item => item.host.test(location.hostname));
  scheduleAutoTranslation();
  if (!adapter) { if (readPreference('cp-translator-custom-site', false)) openGeneral('page', false); return; }
  if (readPreference('cp-translator-custom-site', false) && !rootsForPage().length) { openGeneral('page', false); return; }
  if (rootsForPage().length) openPanel();
  else {
    // Client-rendered statements can arrive after the initial document.
    const detectStatement = () => {
      if (!problemRoots(adapter).length) return;
      pendingObserver.disconnect();
      generalRoot = undefined;
      openPanel();
    };
    pendingObserver = new MutationObserver(detectStatement);
    pendingObserver.observe(document.documentElement, { childList: true, subtree: true });
    if (adapter.waitForStatement) {
      // SPA judges can mount the first statement long after their homepage.
      window.addEventListener('pagehide', () => pendingObserver?.disconnect());
      window.addEventListener('pageshow', () => {
        if (panel) return;
        pendingObserver.observe(document.documentElement, { childList: true, subtree: true });
        detectStatement();
      });
    } else window.setTimeout(() => pendingObserver?.disconnect(), 30000);
  }
})();

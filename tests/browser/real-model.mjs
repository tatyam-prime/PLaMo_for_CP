// Optional end-to-end smoke against the running real PLaMo server. No mock output.
// The default fixture is synthetic prose with captured MathJax markup.
// PLAMO_MODEL=0.8b PLAMO_GENERATED=1 npm run test:real
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const file = process.env.PLAMO_FIXTURE || 'tests/browser/fixtures/codeforces-2120f-opening.html';
const url = process.env.PLAMO_PAGE_URL || 'https://codeforces.com/problemset/problem/2120/F';
const model = process.env.PLAMO_MODEL || '0.8b';
const batchSize = Number(process.env.PLAMO_BATCH || 1);
const api = process.env.PLAMO_API || 'http://127.0.0.1:44894';
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(api) || !Number.isInteger(batchSize) || batchSize < 1 || batchSize > 16) throw new Error('Invalid local QA settings');
const generated = process.env.PLAMO_GENERATED === '1';
const general = process.env.PLAMO_GENERAL === '1';
const rootSelector = process.env.PLAMO_ROOT_SELECTOR || (general ? 'article, main, .markdown-body' : '.md, .lang-en, .problembody, .problem-statement, #problem-statement');
const outputPrefix = process.env.PLAMO_REPORT_PREFIX || '.local/browser-real';
const scriptFile = process.env.PLAMO_SCRIPT || (generated ? 'dist/plamo-for-cp.user.js' : 'browser/translator.js');
const registry = generated ? '' : (await readFile('browser/site-registry.js', 'utf8')).replaceAll('export ', '') + '\n';
const pipeline = generated ? '' : await readFile('plamo_cp/assets/translation-jobs.js', 'utf8') + '\n';
const script = (registry + pipeline + await readFile(scriptFile, 'utf8')).replaceAll('http://127.0.0.1:44894', api);
const html = await readFile(file, 'utf8');
const mathCSS = await readFile(new URL('../../node_modules/katex/dist/katex.min.css', import.meta.url), 'utf8');
const requests = [];
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  await page.route('**/*', route => route.request().url() === url ? route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }) : route.abort());
  await page.exposeFunction('__cpTranslatorHTTP', async request => {
    const target = new URL(request.url);
    if (target.origin !== api || !target.pathname.startsWith('/api/') || target.username || target.password || !['GET', 'POST'].includes(request.method)) throw new Error('QA requests must stay on the local translation API');
    const start = performance.now();
    const response = await fetch(request.url, { method: request.method, headers: request.headers, body: request.data, redirect: 'error' });
    const responseText = await response.text();
    requests.push({ ...request, response: JSON.parse(responseText), status: response.status, elapsed_ms: Math.round(performance.now() - start) });
    console.log(`${response.status} ${request.method} ${request.url} (${requests.at(-1).elapsed_ms}ms)`);
    return { status: response.status, responseText };
  });
  await page.addInitScript(({ selectedModel, batchSize }) => {
    const preferences = new Map([['cp-translator-model', selectedModel], ['cp-translator-batch-size', batchSize], ['cp-translator-supported-batch-sizes', Array.from({ length: 16 }, (_, index) => index + 1)], ['cp-translator-translation-jobs', true]]);
    const listeners = new Map();
    window.readPreference = (key, fallback) => preferences.has(key) ? preferences.get(key) : fallback;
    window.writePreference = (key, value) => { const old = preferences.get(key); preferences.set(key, value); for (const listener of listeners.get(key) || []) listener(key, old, value, false); };
    window.openManagement = () => {};
    window.__cpTranslatorMenus = {};
    window.registerCommand = (label, callback) => { window.__cpTranslatorMenus[label] = callback; };
    window.GM_getValue = window.readPreference;
    window.GM_setValue = window.writePreference;
    window.GM_deleteValue = key => { preferences.delete(key); };
    window.GM_addValueChangeListener = (key, callback) => { const callbacks = listeners.get(key) || []; callbacks.push(callback); listeners.set(key, callbacks); return callbacks.length; };
    window.GM_registerMenuCommand = window.registerCommand;
    window.GM_openInTab = () => {};
    window.requestHTTP = options => {
      let aborted = false;
      window.__cpTranslatorHTTP({ url: options.url, method: options.method, headers: options.headers, data: options.data }).then(response => { if (!aborted) options.onload(response); }).catch(() => { if (!aborted) options.onerror(); });
      return { abort() { aborted = true; options.onabort(); } };
    };
    window.GM_xmlhttpRequest = window.requestHTTP;
  }, { selectedModel: model, batchSize });
  await page.goto(url);
  await page.addStyleTag({ content: mathCSS });
  const before = await page.evaluate(root => ({
    samples: Array.from(document.querySelectorAll('pre'), node => node.outerHTML),
    math: Array.from(document.querySelectorAll('.math, .katex, mjx-container, .MathJax, .tex-span'), node => node.outerHTML),
    literals: Array.from(document.querySelectorAll('code, kbd, samp, .tex-font-style-tt'), node => node.outerHTML),
    rawMath: document.querySelector(root).textContent.match(/\$\$\$[\s\S]*?\$\$\$/g) || [],
    formatting: Array.from(document.querySelector(root).querySelectorAll('strong, em, b, a'), node => ({ tag: node.tagName, href: node.getAttribute('href'), title: node.getAttribute('title') })),
    original: document.querySelector(root)?.textContent,
  }), rootSelector);
  await page.evaluate(script);
  const start = performance.now();
  if (generated && general) await page.evaluate(() => window.__cpTranslatorMenus['このページを翻訳']());
  else {
    if (general) await page.evaluate(() => window.__cpTranslatorMenus['PLaMo: このページの本文を翻訳']());
    const selector = page.getByLabel('モデルサイズ');
    await selector.focus();
    await selector.press(model[0]);
    await selector.press('Tab');
    assert.equal(await selector.inputValue(), model);
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  }
  await page.waitForFunction(() => {
    const shadow = document.querySelector('#cp-translator')?.shadowRoot;
    return shadow && shadow.querySelector('button.primary').disabled === false && shadow.querySelector('.status').textContent.includes('箇所を翻訳しました');
  }, null, { timeout: 600000 });
  const status = await page.locator('#cp-translator').locator('.status').textContent();
  assert.match(status, /箇所を翻訳しました/, status);
  const after = await page.evaluate(root => ({
    samples: Array.from(document.querySelectorAll('pre'), node => node.outerHTML),
    math: Array.from(document.querySelectorAll('.math, .katex, mjx-container, .MathJax, .tex-span'), node => node.outerHTML),
    literals: Array.from(document.querySelectorAll('code, kbd, samp, .tex-font-style-tt'), node => node.outerHTML),
    rawMath: document.querySelector(root).textContent.match(/\$\$\$[\s\S]*?\$\$\$/g) || [],
    formatting: Array.from(document.querySelector(root).querySelectorAll('strong, em, b, a'), node => ({ tag: node.tagName, href: node.getAttribute('href'), title: node.getAttribute('title') })),
    paragraphs: Array.from(document.querySelector(root).querySelectorAll('p[id]'), node => ({ id: node.id, text: node.textContent, html: node.outerHTML })),
    translated: document.querySelector(root)?.textContent,
  }), rootSelector);
  assert.deepEqual(after.samples, before.samples);
  // Japanese word order can move an intact inline formula within a sentence.
  assert.deepEqual([...after.math].sort(), [...before.math].sort());
  assert.deepEqual([...after.literals].sort(), [...before.literals].sort());
  assert.deepEqual([...after.rawMath].sort(), [...before.rawMath].sort());
  assert.deepEqual(after.formatting, before.formatting);
  assert.match(after.translated, /[\u3040-\u30ff]/);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator(rootSelector).first().textContent(), before.original);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  await mkdir('.local', { recursive: true });
  await page.screenshot({ path: outputPrefix + '.png', fullPage: true });
  const report = { fixture: file, url, model, batchSize, generated, status, elapsed_ms: Math.round(performance.now() - start), before, after, requests };
  await writeFile(outputPrefix + '-report.json', JSON.stringify(report, null, 2));
  const translationRequests = requests.filter(request => request.method === 'POST' && /\/(?:translation-jobs|translate)$/.test(new URL(request.url).pathname));
  console.log(JSON.stringify({ status, elapsed_ms: report.elapsed_ms, requests: translationRequests.length, segments: translationRequests.reduce((total, request) => total + JSON.parse(request.data).segments.length, 0), formulas_preserved: before.math.length, samples_preserved: before.samples.length, report: outputPrefix + '-report.json' }));
} finally {
  await browser.close();
}

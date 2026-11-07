import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import katex from 'katex';

const source = await readFile(new URL('../../browser/translator.js', import.meta.url), 'utf8');
const registry = (await readFile(new URL('../../browser/site-registry.js', import.meta.url), 'utf8')).replaceAll('export const ', 'const ').replaceAll('export function ', 'function ');
let browser;

before(async () => {
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
});
after(async () => { await browser?.close(); });

async function fixture(html, url = 'https://codeforces.com/problemset/problem/1/A', options = {}) {
  const page = await browser.newPage();
  if (options.clock) await page.clock.install();
  await page.route(url, route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }));
  await page.addInitScript(({ options }) => {
    window.__cpTranslatorRequests = [];
    window.__cpTranslatorCommands = {};
    window.__cpTranslatorPreferences = { ...(options.preferences || {}) };
    window.readPreference = (key, fallback) => {
      if (key === 'cp-translator-current-site') {
        if (window.__cpTranslatorPreferences[key] !== undefined) return window.__cpTranslatorPreferences[key];
        const site = window.__cpTranslatorSiteRegistry.find(site => site.host.test(location.hostname) && window.__cpTranslatorProblemRoots(site).length);
        return site && !window.__cpTranslatorPreferences['cp-translator-disabled-sites']?.includes(site.id) ? site.id : null;
      }
      return window.__cpTranslatorPreferences[key] ?? fallback;
    };
    window.writePreference = (key, value) => { window.__cpTranslatorPreferences[key] = value; };
    window.openManagement = () => { window.__managementOpened = (window.__managementOpened || 0) + 1; };
    window.registerCommand = (name, callback) => { window.__cpTranslatorCommands[name] = callback; };
    window.requestHTTP = details => {
      const payload = details.data ? JSON.parse(details.data) : null;
      window.__cpTranslatorRequests.push({ url: details.url, headers: details.headers, payload });
      let cancelled = false;
      const timer = setTimeout(() => {
        if (cancelled) return;
        if (!payload) { details.onload({ status: 200, responseText: '{"status":"ok","app":"plamo-for-cp"}' }); return; }
        if (options.networkError) { details.onerror(); return; }
        if (options.httpError) { details.onload({ status: 422, responseText: JSON.stringify({ detail: '保護マーカーが欠けています' }) }); return; }
        const translations = payload.segments.map(segment => {
          let text = options.missingMarker || (options.missingFirstMarker && segment.id === '0') ? '壊れた訳文' : (options.literalHtml ? '<img src=x onerror=alert(1)>' : '日本語: ') + segment.text.replaceAll('You are given', '与えられるのは').replaceAll('positive integers', '正整数').replaceAll('Find the answer', '答えを求めよ');
          if (options.translations?.[segment.id] !== undefined) text = options.translations[segment.id];
          if (options.tagCorruption === 'missing') text = text.replace('</plm-a>', '');
          else if (options.tagCorruption === 'duplicate') text = text.replace('<plm-a>', '<plm-a><plm-a>');
          else if (options.tagCorruption === 'unknown') text = text.replace('<plm-a>', '<plm-z>');
          else if (options.tagCorruption === 'crossed') text = text.replace('</plm-b></plm-a>', '</plm-a></plm-b>');
          return { id: segment.id, text };
        });
        const errors = payload.segments.filter(segment => (options.errorIds || []).includes(segment.id) || options.rejectedSources?.includes(segment.text) || options.markerLimit && (segment.text.match(/\[\[PLM_\d+\]\]/g) || []).length > options.markerLimit).map(segment => ({ id: segment.id, message: 'この段落の数式を保持できなかったため原文を残します。' }));
        details.onload({ status: 200, responseText: JSON.stringify({ translations: translations.filter(item => !errors.some(error => error.id === item.id)), errors, cached: options.cached || 0, model: payload.model, precision: payload.precision }) });
      }, payload ? (options.delay || 5) : 1);
      return { abort() { cancelled = true; clearTimeout(timer); details.onabort(); } };
    };
  }, { options });
  await page.goto(url);
  const policy = options.policyMessage ? `SITE_REGISTRY.push({ id: 'policy-fixture', name: 'Policy fixture', host: /^policy\\.example$/, roots: 'article', sourceLanguage: 'English', restriction: () => ${JSON.stringify(options.policyMessage)} });\n` : '';
  await page.evaluate(registry + '\n' + policy + 'window.__cpTranslatorSiteRegistry = SITE_REGISTRY; window.__cpTranslatorProblemRoots = problemRoots;\n' + source);
  return page;
}

async function translate(page) {
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
}

async function chooseModel(page, model) {
  const select = page.getByLabel('モデルサイズ');
  await select.focus(); await select.press(model[0]); await select.press('Tab');
  assert.equal(await select.inputValue(), model);
}

async function assertNoOriginalToggle(page) {
  assert.equal(await page.getByRole('button', { name: '原文を表示', exact: true }).count(), 0);
  const toggle = page.getByRole('button', { name: '原文を表示', exact: true, includeHidden: true });
  assert.equal(await toggle.isHidden(), true);
  assert.equal(await toggle.isDisabled(), true);
}

test('Codeforces: paragraph context, math, inline formatting, links, sample bytes and original DOM survive', async () => {
  const html = `<div class="problem-statement"><div class="header"><div class="title">A. Title</div><div>time limit per test</div></div><p id="paragraph">You are given <span class="MathJax" id="formula"><span aria-hidden="true">n + m</span></span> positive integers and <strong>Find the answer</strong>. See <a href="/help" id="link">the statement</a> <code>YES</code>.</p><div class="input-specification"><div class="section-title">Input</div><p>Find the answer.</p></div><div class="sample-test"><div>Input</div><pre id="sample">1  2\n\n3 &lt; 4\n</pre></div></div>`;
  const page = await fixture(html);
  await page.evaluate(() => {
    window.__originalParagraphNodes = Array.from(document.querySelector('#paragraph').childNodes);
    window.__originalFormula = document.querySelector('#formula').outerHTML;
    window.__originalSample = document.querySelector('#sample').innerHTML;
    document.querySelector('#link').addEventListener('custom-event', () => { window.__linkEvent = true; });
  });
  assert.equal(await page.getByLabel('モデルサイズ').inputValue(), '2b');
  await chooseModel(page, '8b');
  await translate(page);
  assert.match(await page.locator('#paragraph').textContent(), /日本語/);
  assert.equal(await page.locator('#formula').evaluate(node => node.outerHTML), await page.evaluate(() => window.__originalFormula));
  assert.equal(await page.locator('#sample').evaluate(node => node.innerHTML), await page.evaluate(() => window.__originalSample));
  assert.equal(await page.locator('#paragraph strong').textContent(), '答えを求めよ');
  assert.equal(await page.locator('#paragraph a').getAttribute('href'), '/help');
  assert.equal(await page.locator('#paragraph code').textContent(), 'YES');
  assert.equal(await page.locator('.header').textContent(), 'A. Titletime limit per test');
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload));
  assert.equal(requests[0].payload.model, '8b');
  assert.equal(requests[0].payload.precision, '4bit');
  assert.equal(requests[0].payload.document_type, 'problem');
  assert.equal(requests[0].headers['X-Plamo-Client'], 'userscript');
  assert.match(requests[0].payload.segments[0].text, /You are given \[\[PLM_0\]\] positive integers/);
  assert.ok(!requests.some(request => request.payload.segments[0].text.includes('1  2')));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__originalParagraphNodes.every((node, index) => document.querySelector('#paragraph').childNodes[index + 1] === node)), true);
  await page.locator('#link').dispatchEvent('custom-event');
  assert.equal(await page.evaluate(() => window.__linkEvent), true);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.match(await page.locator('#paragraph').textContent(), /日本語/);
  await page.close();
});

test('raw TeX is protected before MathJax renders', async () => {
  const page = await fixture(String.raw`<div class="problem-statement"><p id="p">Find the answer for $$$n \le 10^9$$$ and \(x+y\) and $z$.</p></div>`);
  const original = await page.locator('#p').textContent();
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.find(request => request.payload).payload.segments[0].text);
  assert.ok(!sent.includes('10^9'));
  assert.equal((sent.match(/\[\[PLM_/g) || []).length, 3);
  assert.equal(await page.locator('#p').textContent(), `日本語: ${original.replace('Find the answer', '答えを求めよ')}`);
  await page.close();
});

test('Codeforces2255B rendered MathJax siblings use one slot per formula and restore every original node', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-mathjax2-example.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/contest/2255/problem/B');
  await page.evaluate(() => { window.__mathJaxOriginal = [...document.querySelector('.problem-statement p').childNodes]; });
  const math = await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  assert.equal(math.length, 12);
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.find(request => request.payload).payload.segments[0].text);
  assert.equal((sent.match(/\[\[PLM_\d+\]\]/g) || []).length, 4);
  assert.match(sent, /In this example, \[\[PLM_0\]\] is a sample value associated with \[\[PLM_1\]\]/);
  assert.deepEqual(await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('.problem-statement p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__mathJaxOriginal[index])), true);
  await page.close();
});

test('Codeforces2120F retries a rejected math-only chunk once at sentence boundaries while preserving its footnote and original IDs', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2120f-opening.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/problemset/problem/2120/F', { markerLimit: 4, preferences: { 'cp-translator-translation-previews': true } });
  const original = await page.locator('#target').textContent(), math = await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await page.evaluate(() => { window.__openingNodes = [...document.querySelector('#target').childNodes]; });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.deepEqual(sent.map(text => (text.match(/\[\[PLM_\d+\]\]/g) || []).length), [7, 4, 3]);
  assert.equal(sent[1] + sent[2], sent[0]);
  assert.deepEqual(sent[2].match(/\[\[PLM_\d+\]\]/g), ['[[PLM_4]]', '[[PLM_5]]', '[[PLM_6]]']);
  assert.equal((await page.locator('#target').textContent()).match(/日本語:/g).length, 2);
  assert.deepEqual(await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  assert.equal(await page.locator('#MathJax-Element-35').textContent(), '^{\\text{‡}}');
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#target').textContent(), original);
  assert.equal(await page.locator('#target').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__openingNodes[index])), true);
  await page.close();
});

test('failed sentence recovery is bounded to one pass and never loosens math validation', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2120f-opening.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/problemset/problem/2120/F', { httpError: true, preferences: { 'cp-translator-translation-previews': true } });
  const original = await page.locator('#target').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#target').evaluate(node => node.outerHTML), original);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 3);
  await assertNoOriginalToggle(page);
  await page.close();
});

test('legacy translation retries balanced sentences before submitting the next primary paragraph', async () => {
  const page = await fixture('<div class="problem-statement"><p>Round <strong>all</strong> symbols use <var>n</var>. Square symbols use <var>m</var>.</p><p>A later primary paragraph.</p></div>', undefined, { markerLimit: 1 });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments));
  assert.deepEqual(sent.map(segment => segment.id), ['0', '2', '3', '1']);
  assert.equal(sent[1].text + sent[2].text, sent[0].text);
  assert.equal(sent[3].text, 'A later primary paragraph.');
  await page.close();
});

for (const count of [4, 6]) test(`a failed ${count}-sentence paragraph retains context in two halves before splitting only the failed half`, async () => {
  const names = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot'].slice(0, count);
  const parts = names.map((name, index) => `${name} case uses [[PLM_${index}]].${index === count - 1 ? '' : ' '}`);
  const middle = count / 2, left = parts.slice(0, middle).join(''), right = parts.slice(middle).join('');
  const html = `<div class="problem-statement"><p id="p">${names.map((name, index) => `${name} case uses <var>${index}</var>.${index === count - 1 ? '' : ' '}`).join('')}</p></div>`;
  const page = await fixture(html, undefined, { markerLimit: middle, rejectedSources: [right] });
  const original = await page.locator('#p').textContent();
  await page.locator('#p').evaluate(node => { window.__halvesOriginal = [...node.childNodes]; });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments));
  assert.equal(sent[0].text, parts.join(''));
  assert.deepEqual(sent.slice(1, 3).map(segment => segment.text), [left, right]);
  assert.deepEqual(sent.slice(3).map(segment => segment.text), parts.slice(middle));
  assert.equal(sent.filter(segment => segment.text === left).length, 1, 'a verified half must not be queued again');
  assert.equal(sent[1].text + sent[2].text, sent[0].text);
  assert.equal(sent.slice(3).map(segment => segment.text).join(''), right);
  assert.equal((await page.locator('#p').textContent()).match(/日本語:/g).length, 1 + middle);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), original);
  assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__halvesOriginal[index])), true);
  await page.close();
});

test('two-level fallback terminates when both halves and every sentence fail', async () => {
  const names = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot'];
  const page = await fixture(`<div class="problem-statement"><p id="p">${names.map(name => `${name} case uses <var>n</var>.`).join(' ')}</p></div>`, undefined, { httpError: true });
  const original = await page.locator('#p').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), original);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 9);
  await assertNoOriginalToggle(page);
  await page.close();
});

test('MathJax grouping does not consume an unrelated source script or intervening prose', async () => {
  const page = await fixture('<div class="problem-statement"><p>Find <span class="MathJax_Preview"></span><span class="MathJax" id="MathJax-Element-1-Frame">n</span> intervening prose <script type="math/tex" id="MathJax-Element-2">m</script>.</p></div>');
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.find(request => request.payload).payload.segments[0].text);
  assert.equal((sent.match(/\[\[PLM_\d+\]\]/g) || []).length, 3);
  assert.match(sent, /intervening prose/);
  await page.close();
});

test('Codeforces2273D six-dollar display TeX stays one opaque slot without visual inline padding', async () => {
  const formula = String.raw`$$$$$$ \min\bigl(|x-a_i|,\ |x-b_i|\bigr) $$$$$$`;
  const page = await fixture(`<div class="problem-statement"><p id="p">You then lose ${formula} aura.</p></div>`, 'https://codeforces.com/contest/2273/problem/D', { translations: { 0: '失う量は[[PLM_0]]オーラです。' } });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.find(request => request.payload).payload);
  assert.equal(sent.segments[0].text, 'You then lose [[PLM_0]] aura.');
  assert.equal(sent.source_context, '');
  assert.equal(await page.locator('#p').textContent(), `失う量は${formula}オーラです。`);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), `You then lose ${formula} aura.`);
  await page.close();
});

test('actual KaTeX HTML+MathML rendering is preserved byte-for-byte', async () => {
  const inline = katex.renderToString(String.raw`\sum_{i=1}^{n} \frac{a_i^2}{i+1} \le 10^9`, { output: 'htmlAndMathml' });
  const display = katex.renderToString(String.raw`\begin{pmatrix} a & b \\ c & d \end{pmatrix}`, { displayMode: true, output: 'htmlAndMathml' });
  const page = await fixture(`<div class="problem-statement"><p id="p">Find the answer ${inline}.</p><p id="display">You are given ${display}.</p></div>`);
  await page.evaluate(() => { window.__katexNodes = Array.from(document.querySelectorAll('.katex, .katex-display')); });
  const before = await page.locator('.katex, .katex-display').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  assert.equal(await page.locator('.katex math annotation').count(), 2);
  await translate(page);
  assert.deepEqual(await page.locator('.katex, .katex-display').evaluateAll(nodes => nodes.map(node => node.outerHTML)), before);
  const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload.segments[0].text));
  assert.ok(sources.every(text => !text.includes('sum') && !text.includes('pmatrix')));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__katexNodes.every((node, index) => document.querySelectorAll('.katex, .katex-display')[index] === node)), true);
  await page.close();
});

test('missing inline variable separators become copyable ASCII spaces without changing formulas or original nodes', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">There are <var id="v">n</var> integers.</p><p>Next paragraph.</p></div>', undefined, { translations: { 0: '整数[[PLM_0]]個', 1: '次の段落。' } });
  await page.evaluate(() => { window.__gapOriginal = [...document.querySelector('#p').childNodes]; });
  const original = await page.locator('#v').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#p').textContent(), '整数 n 個');
  assert.equal(await page.locator('#v').evaluate(node => node.outerHTML), original);
  assert.deepEqual(await page.locator('#v').evaluate(node => [node.parentElement.id, node.previousSibling.nodeValue, node.nextSibling.nodeValue]), ['p', ' ', ' ']);
  const copied = await page.locator('#p').evaluate(node => { const selection = getSelection(), range = document.createRange(); range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range); const text = selection.toString(); selection.removeAllRanges(); return text; });
  assert.equal(copied, '整数 n 個');
  const payload = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload)[1].payload);
  assert.equal(payload.history, undefined);
  assert.ok(!payload.source_context.includes('整数'));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), 'There are n integers.');
  assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__gapOriginal[index])), true);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), copied);
  await page.close();
});

test('inline separators preserve existing whitespace and distinguish separate math from punctuation and other protected nodes', async () => {
  const page = await fixture('<div class="problem-statement"><p id="punct">integer <var>n</var> .</p><p id="spaced">integer <code>n</code> count</p><p id="adjacent">value <var>x</var> <var>y</var> count</p><p id="display">equation <span class="katex-display"><span class="katex">x</span></span> text</p><p id="image">icon <img alt="x"> label</p><p id="script">value <script type="math/tex">x_i</script> text</p><p id="joined">integer<var>n</var>count</p></div>', undefined, { translations: { 0: '整数[[PLM_0]]。', 1: '整数 [[PLM_0]] 個', 2: '値[[PLM_0]][[PLM_1]]個', 3: '式[[PLM_0]]本文', 4: '印[[PLM_0]]説明', 5: '値[[PLM_0]]本文', 6: '整数[[PLM_0]]個' } });
  const originalDisplay = await page.locator('.katex-display').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#punct').textContent(), '整数 n。');
  assert.equal(await page.locator('#spaced').textContent(), '整数 n 個');
  assert.equal(await page.locator('#adjacent').textContent(), '値 x y 個');
  assert.equal(await page.locator('#adjacent').evaluate(node => { const [left, right] = node.querySelectorAll('var'); let text = ''; for (let next = left.nextSibling; next !== right; next = next.nextSibling) text += next.textContent; return text; }), ' ');
  for (const [id, expected] of Object.entries({ display: '式x本文', image: '印説明', script: '値x_i本文', joined: '整数n個' })) assert.equal(await page.locator('#' + id).textContent(), expected);
  assert.equal(await page.locator('.katex-display').evaluate(node => node.outerHTML), originalDisplay);
  await page.close();
});

test('real separators look through nested formatting and hidden MathJax bookkeeping without changing renderer subtrees', async () => {
  const page = await fixture('<div class="problem-statement"><p id="wrapped">integer <strong><var>n</var></strong> count</p><p id="jax">integer <span class="MathJax_Preview" style="display:none">n</span><span class="MathJax">n</span><script type="math/tex">n</script> count</p></div>', undefined, { translations: { 0: '整数<plm-a>[[PLM_0]]</plm-a>個', 1: '整数[[PLM_0]][[PLM_1]][[PLM_2]]個' } });
  const original = await page.locator('#jax .MathJax').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#wrapped').textContent(), '整数 n 個');
  assert.equal(await page.locator('#wrapped strong').textContent(), ' n ');
  assert.equal(await page.locator('#jax').innerText(), '整数 n 個');
  assert.equal(await page.locator('#wrapped strong').count(), 1);
  assert.equal(await page.locator('#jax .MathJax').evaluate(node => node.outerHTML), original);
  assert.deepEqual(await page.locator('#jax .MathJax').evaluate(node => [node.previousSibling.nodeValue, node.nextSibling.nodeValue]), [' ', ' ']);
  await page.close();
});

test('independent Codeforces MathJax groups restore one real separator across formatting and marker reordering', async () => {
  const math = (index, text) => `<span class="MathJax_Preview" style="display:none">${text}</span><span class="MathJax" id="MathJax-Element-${index}-Frame">${text}</span><script type="math/tex" id="MathJax-Element-${index}">${text}</script>`;
  const html = `<div class="problem-statement"><p id="separated">value ${math(1, 'n')} <strong>${math(2, 'm')}</strong> count</p><p id="joined-pair">value ${math(3, 'a')}${math(4, 'b')} count</p><p id="already-spaced">value ${math(5, 'x')} ${math(6, 'y')} count</p></div>`;
  const page = await fixture(html, 'https://codeforces.com/problemset/problem/1/A', { translations: { 0: '値<plm-a>[[PLM_1]]</plm-a>[[PLM_0]]個', 1: '数[[PLM_1]][[PLM_0]]個', 2: '値 [[PLM_0]] [[PLM_1]] 個' } });
  await page.evaluate(() => { window.__groupOriginals = [...document.querySelectorAll('.problem-statement p')].map(node => [...node.childNodes]); });
  const formulas = await page.locator('.MathJax_Preview,.MathJax,script[type="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML).sort());
  await translate(page);
  assert.equal(await page.locator('#separated').innerText(), '値 m n 個');
  assert.equal(await page.locator('#joined-pair').innerText(), '数ba個');
  assert.equal(await page.locator('#already-spaced').innerText(), '値 x y 個');
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.ok(sent.every(text => (text.match(/\[\[PLM_\d+\]\]/g) || []).length === 2));
  assert.deepEqual(await page.locator('.MathJax_Preview,.MathJax,script[type="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML).sort()), formulas);
  assert.equal(await page.locator('.MathJax').evaluateAll(nodes => nodes.every(node => node.previousSibling.nodeType === Node.ELEMENT_NODE && node.previousSibling.matches('.MathJax_Preview') && node.nextSibling.nodeType === Node.ELEMENT_NODE && node.nextSibling.id === node.id.replace('-Frame', ''))), true);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('.problem-statement p').evaluateAll(nodes => nodes.every((node, index) => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, childIndex) => child === window.__groupOriginals[index][childIndex]))), true);
  await page.close();
});

test('raw inline TeX gets ASCII separators while display TeX and every original TeX string stay unchanged', async () => {
  const page = await fixture(String.raw`<div class="problem-statement"><p id="inline">variable \(x_i\) count</p><p id="display">equation $$x_i$$ text</p><p id="display-bracket">equation \[x_i\] text</p></div>`, undefined, { translations: { 0: '変数[[PLM_0]]個', 1: '式[[PLM_0]]本文', 2: '式[[PLM_0]]本文' } });
  await translate(page);
  assert.equal(await page.locator('#inline').textContent(), String.raw`変数 \(x_i\) 個`);
  assert.equal(await page.locator('#display').textContent(), String.raw`式$$x_i$$本文`);
  assert.equal(await page.locator('#display-bracket').textContent(), String.raw`式\[x_i\]本文`);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#inline').textContent(), String.raw`variable \(x_i\) count`);
  await page.close();
});

test('compact toolbar shows attribution, icons and a small model selector, and opens management explicitly', async () => {
  const page = await fixture('<div class="problem-statement"><p>Find the answer.</p></div>');
  const size = await page.locator('#cp-translator').evaluate(node => ({ width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height }));
  assert.ok(size.width <= 210 && size.height <= 54);
  assert.equal(await page.getByText('Translated by PLaMo 3 Translate', { exact: true }).isVisible(), true);
  assert.equal(await page.getByRole('button', { name: '原文を表示', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '停止', exact: true }).count(), 0);
  await chooseModel(page, '0.8b');
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  assert.equal(await page.locator('option[value="0.8b"]').textContent(), '0.8B');
  await page.getByRole('button', { name: '設定・管理', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__managementOpened), 1);
  await translate(page);
  assert.equal(await page.getByRole('button', { name: '原文を表示', exact: true }).count(), 1);
  assert.ok((await page.locator('#cp-translator').boundingBox()).width <= 210);
  await page.screenshot({ path: '.local/compact-toolbar.png' });
  await page.close();
});

test('nested inline formatting uses unique alphabetic tags and reconstructs native elements', async () => {
  const spans = Array.from({ length: 28 }, (_, index) => `<strong>word${index}</strong>`).join(' ');
  const page = await fixture(`<div class="problem-statement"><p id="nested">Find <strong>the <em>smallest</em></strong> integer <span class="katex">n</span>.</p><p id="many">Find ${spans}.</p></div>`);
  await translate(page);
  const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload.segments[0].text));
  assert.match(sources[0], /<plm-a>the <plm-b>smallest<\/plm-b><\/plm-a>/);
  assert.match(sources[0], /\[\[PLM_0\]\]/);
  assert.match(sources[1], /<plm-z>word25<\/plm-z>/);
  assert.match(sources[1], /<plm-aa>word26<\/plm-aa>/);
  assert.match(sources[1], /<plm-ab>word27<\/plm-ab>/);
  assert.equal(await page.locator('#nested strong em').textContent(), 'smallest');
  assert.equal(await page.locator('#many strong').count(), 28);
  assert.equal(await page.locator('plm-a').count(), 0);
  await page.close();
});

test('Codeforces italic and bold prose translate as formatting while nested math remains opaque', async () => {
  const html = '<style>.tex-font-style-it{font-style:italic}.tex-font-style-bf{font-weight:700}</style><div class="problem-statement"><p id="styled"><span class="tex-font-style-it" id="italic">Find the answer</span> using <span class="tex-font-style-bf" id="bold">positive integers</span> and <span class="tex-span" id="math"><span class="tex-font-style-it">x</span><sub>i</sub><span class="tex-font-style-bf">n</span></span>.</p><p id="plain"><i id="i">Find the answer</i> using <em id="em">positive integers</em>.</p><p id="only"><span class="tex-font-style-it">Find the answer</span></p></div>';
  const page = await fixture(html);
  const formula = await page.locator('#math').evaluate(node => node.outerHTML);
  await page.evaluate(() => { window.__styledOriginal = [...document.querySelector('#styled').childNodes]; });
  await translate(page);
  const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sources.length, 3);
  assert.match(sources[0], /<plm-a>Find the answer<\/plm-a> using <plm-b>positive integers<\/plm-b> and \[\[PLM_0\]\]/);
  assert.match(sources[2], /<plm-a>Find the answer<\/plm-a>/);
  assert.equal(await page.locator('#italic').textContent(), '答えを求めよ');
  assert.equal(await page.locator('#bold').textContent(), '正整数');
  assert.equal(await page.locator('#i').textContent(), '答えを求めよ');
  assert.equal(await page.locator('#em').textContent(), '正整数');
  assert.equal(await page.locator('#only .tex-font-style-it').textContent(), '答えを求めよ');
  assert.equal(await page.locator('#italic').evaluate(node => getComputedStyle(node).fontStyle), 'italic');
  assert.equal(await page.locator('#bold').evaluate(node => getComputedStyle(node).fontWeight), '700');
  assert.equal(await page.locator('#math').evaluate(node => node.outerHTML), formula);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#styled').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__styledOriginal[index])), true);
  assert.equal(await page.locator('#italic').textContent(), 'Find the answer');
  assert.equal(await page.locator('#math').evaluate(node => node.outerHTML), formula);
  await page.close();
});

test('Codeforces2261H actual style classes translate italic prose and preserve first/second input tokens', async () => {
  // Verified in the rendered DOM of https://codeforces.com/contest/2261/problem/H.
  const html = '<style>.tex-font-style-it{font-style:italic}.tex-font-style-tt{font-family:monospace}</style><div class="problem-statement"><p id="intro"><span class="tex-font-style-it">This is a communication problem.</span></p><p id="first">The first line contains <span class="tex-font-style-tt">first</span>.</p><p id="second">The first line contains <span class="tex-font-style-tt">second</span>.</p><p id="bits">The value is <span class="tex-font-style-tt">0</span> or <span class="tex-font-style-tt">1</span> on node $$$u$$$.</p></div>';
  const page = await fixture(html, 'https://codeforces.com/contest/2261/problem/H', { translations: { 0: '<plm-a>これは通信問題です。</plm-a>', 1: '最初の行には [[PLM_0]] が含まれます。', 2: '最初の行には [[PLM_0]] が含まれます。' } });
  const literals = await page.locator('.tex-font-style-tt').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await translate(page);
  const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sources[0], '<plm-a>This is a communication problem.</plm-a>');
  assert.equal(sources[1], 'The first line contains [[PLM_0]].');
  assert.equal(sources[2], 'The first line contains [[PLM_0]].');
  assert.equal(sources[3], 'The value is [[PLM_0]] or [[PLM_1]] on node [[PLM_2]].');
  assert.equal(await page.locator('#intro .tex-font-style-it').textContent(), 'これは通信問題です。');
  assert.equal(await page.locator('#intro .tex-font-style-it').evaluate(node => getComputedStyle(node).fontStyle), 'italic');
  assert.deepEqual(await page.locator('.tex-font-style-tt').evaluateAll(nodes => nodes.map(node => node.outerHTML)), literals);
  assert.match(await page.locator('#bits').textContent(), /\$\$\$u\$\$\$/);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#intro').textContent(), 'This is a communication problem.');
  assert.deepEqual(await page.locator('.tex-font-style-tt').evaluateAll(nodes => nodes.map(node => node.outerHTML)), literals);
  await page.close();
});

test('Codeforces2196D emphasis and a grouped footnote stay in whole-sentence requests and restore exact source nodes', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2196d-bold.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/contest/2196/problem/D', { translations: {
    0: 'この集まりの<plm-a>すべて</plm-a>の丸記号を確認する[[PLM_0]];',
    1: 'この集まりの<plm-a>すべて</plm-a>の角記号を確認する。',
  } });
  const original = await page.locator('li').allTextContents();
  const math = await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await page.locator('li').evaluateAll(nodes => { window.__boldOriginal = nodes.map(node => [...node.childNodes]); });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.deepEqual(sent, [' Review <plm-a>all</plm-a> round symbols in this collection[[PLM_0]]; ', ' Review <plm-a>all</plm-a> square symbols in this collection. ']);
  assert.deepEqual(await page.locator('.tex-font-style-bf').allTextContents(), ['すべて', 'すべて']);
  assert.equal(await page.locator('.tex-font-style-bf').first().evaluate(node => getComputedStyle(node).fontWeight), '700');
  assert.deepEqual(await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"]').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.deepEqual(await page.locator('li').allTextContents(), original);
  assert.equal(await page.locator('li').evaluateAll(nodes => nodes.every((node, row) => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, column) => child === window.__boldOriginal[row][column]))), true);
  await page.close();
});

test('a rejected Codeforces2196D sentence keeps its bold quantifier and footnote original without clause retries', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2196d-bold.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/contest/2196/problem/D', { tagCorruption: 'missing' });
  const original = await page.locator('li').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await translate(page);
  assert.deepEqual(await page.locator('li').evaluateAll(nodes => nodes.map(node => node.outerHTML)), original);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sent.length, 2);
  assert.ok(sent.every(text => text.startsWith(' Review <plm-a>all</plm-a> ') && text.includes('symbols in this collection')));
  await assertNoOriginalToggle(page);
  await page.close();
});

for (const zero of ['math', 'code']) test(`Codeforces2089B2 keeps a single quantified sentence and its protected zero (${zero}) intact`, async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2089b2-output.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/contest/2089/problem/B2', { translations: { 0: '[[PLM_3]]にちょうど[[PLM_2]]回の編集をした後、[[PLM_0]]の各値が[[PLM_1]]になる最小コストを求める。' } });
  if (zero === 'code') await page.evaluate(() => {
    const script = document.querySelector('#MathJax-Element-45'), renderer = script.previousSibling, preview = renderer.previousSibling;
    const literal = document.createElement('code'); literal.id = 'literal-zero'; literal.textContent = '0'; preview.before(literal);
    script.remove(); renderer.remove(); preview.remove();
  });
  const original = await page.locator('#output').textContent();
  const protectedNodes = await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"],code').evaluateAll(nodes => nodes.map(node => node.outerHTML).sort());
  await page.locator('#output').evaluate(node => { window.__outputOriginal = [...node.childNodes]; });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.deepEqual(sent, ['Return the smallest cost for every value in [[PLM_0]] to reach [[PLM_1]] after exactly [[PLM_2]] allowed edits to [[PLM_3]].']);
  assert.deepEqual(await page.locator('.MathJax_Preview,.MathJax,script[type^="math/tex"],code').evaluateAll(nodes => nodes.map(node => node.outerHTML).sort()), protectedNodes);
  assert.match(await page.locator('#output').textContent(), /ちょうど/);
  assert.doesNotMatch(await page.locator('#output').textContent(), /PLM_|<math-/);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#output').textContent(), original);
  assert.equal(await page.locator('#output').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__outputOriginal[index])), true);
  await page.close();
});

test('a rejected single math sentence stays original after one attempt', async () => {
  const html = await readFile(new URL('./fixtures/codeforces-2089b2-output.html', import.meta.url), 'utf8');
  const page = await fixture(html, 'https://codeforces.com/contest/2089/problem/B2', { markerLimit: 3 });
  const original = await page.locator('#output').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#output').evaluate(node => node.outerHTML), original);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 1);
  await assertNoOriginalToggle(page);
  await page.close();
});

test('balanced sentence recovery retains nested emphasis and never cuts inside its formatting scope', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p"><strong>A nested <em>clause ends here.</em> All round symbols use <var>n</var>.</strong> Square symbols use <var>m</var>.</p></div>', undefined, { markerLimit: 1 });
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sent.length, 3);
  assert.equal(sent[1] + sent[2], sent[0]);
  assert.equal(sent[1], '<plm-a>A nested <plm-b>clause ends here.</plm-b> All round symbols use [[PLM_0]].</plm-a> ');
  assert.equal(sent[2], 'Square symbols use [[PLM_1]].');
  assert.equal(await page.locator('#p strong em').count(), 1);
  assert.equal((await page.locator('#p').textContent()).match(/日本語:/g).length, 2);
  await page.close();
});

for (const corruption of ['missing', 'duplicate', 'unknown', 'crossed']) {
  test(`${corruption} formatting tags keep the whole sentence original instead of translating hanging clauses`, async () => {
    const page = await fixture('<div class="problem-statement"><p id="p">Find <strong>the <em>smallest</em></strong> integer.</p></div>', undefined, { tagCorruption: corruption });
    const before = await page.locator('#p').evaluate(node => node.outerHTML);
    await translate(page);
    assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), before);
    assert.equal(await page.locator('#p strong em').count(), 1);
    const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
    assert.equal(sources.length, 1);
    assert.match(sources[0], /^Find <plm-a>the <plm-b>smallest<\/plm-b><\/plm-a> integer\.$/);
    await assertNoOriginalToggle(page);
    await page.close();
  });
}

test('missing math markers fail safely without replacing the original', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Find the answer <span class="katex">n</span>.</p></div>', undefined, { missingMarker: true });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('マーカー'));
  assert.equal(await page.locator('#p').textContent(), 'Find the answer n.');
  await assertNoOriginalToggle(page);
  await page.close();
});

test('long paragraph chunks display progressively, buffer in original view and restore native identities', async () => {
  const first = 'You are given <strong>positive integers</strong> and must choose a valid strategy that accounts for every earlier decision without changing the underlying problem definition. ';
  const second = 'The next sentence contains <var id="v">n</var> and explains that each choice must be made before the next result is available, while preserving all information collected in the previous rounds. ';
  const third = 'The final sentence remains pending until its own translation finishes, and the original mathematical variables must remain exactly as the author provided them in the problem.';
  const page = await fixture(`<div class="problem-statement"><p id="p">${first}${second}${third}</p></div>`, undefined, { delay: 180 });
  await page.evaluate(() => { window.__longOriginal = [...document.querySelector('#p').childNodes]; });
  const original = await page.locator('#p').textContent(), formula = await page.locator('#v').evaluate(node => node.outerHTML);
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語: ') && !document.querySelector('#cp-translator').shadowRoot.querySelector('button[aria-label="停止"]').hidden);
  assert.ok((await page.locator('#p').textContent()).endsWith(third));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), original);
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
  assert.equal(await page.locator('#p').textContent(), original);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.equal((await page.locator('#p').textContent()).match(/日本語:/g).length, 3);
  assert.equal(await page.locator('#v').evaluate(node => node.outerHTML), formula);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sent.length, 3); assert.equal(sent.join(''), first.replace('<strong>', '<plm-a>').replace('</strong>', '</plm-a>') + second.replace('<var id="v">n</var>', '[[PLM_0]]') + third);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__longOriginal[index])), true);
  await page.close();
});

test('stream-capable clients use600-character paragraph chunks while other clients keep300', async () => {
  const sentence = 'This sentence explains the complete strategy and the conditions that must be met before any decision is made, while retaining all relevant details from previous rounds. ';
  for (const [previews, expected] of [[false, 6], [true, 2]]) {
    const page = await fixture(`<div class="problem-statement"><p>${sentence.repeat(6)}</p></div>`, undefined, { preferences: { 'cp-translator-translation-previews': previews } });
    await translate(page);
    const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
    assert.equal(sent.length, expected); assert.equal(sent.join(''), sentence.repeat(6));
    await page.close();
  }
});

test('a page edit inside an already displayed paragraph chunk rejects later results instead of overwriting it', async () => {
  const sentence = 'This sentence explains the complete strategy and the conditions that must be met before any decision is made, while retaining all relevant details from previous rounds. ';
  const page = await fixture(`<div class="problem-statement"><p id="p">${sentence.repeat(3)}</p></div>`, undefined, { delay: 150 });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語: '));
  await page.locator('#p').evaluate(node => { node.firstChild.nextSibling.textContent = 'Page-authored replacement.'; });
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('ページの内容が更新'));
  assert.match(await page.locator('#p').textContent(), /Page-authored replacement/);
  await page.close();
});

test('a rejected formatted single sentence is never retried as isolated words or hanging clauses', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Find <strong>the answer</strong> for <var id="v">n</var>.</p><p>Continue with another paragraph.</p></div>', undefined, { missingMarker: true });
  const original = await page.locator('#p').evaluate(node => node.outerHTML);
  await translate(page);
  assert.equal(await page.locator('#p').evaluate(node => node.outerHTML), original);
  const sources = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).flatMap(request => request.payload.segments.map(segment => segment.text)));
  assert.equal(sources.length, 2);
  assert.equal(sources[0], 'Find <plm-a>the answer</plm-a> for [[PLM_0]].');
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /原文を保持/);
  await page.close();
});

test('Stop keeps completed paragraph chunks switchable and leaves later chunks original', async () => {
  const sentence = 'This sentence explains the complete strategy and the conditions that must be met before any decision is made, while retaining all relevant details from previous rounds. ';
  const page = await fixture(`<div class="problem-statement"><p id="p">${sentence.repeat(3)}</p></div>`, undefined, { delay: 150 });
  const original = await page.locator('#p').textContent();
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語: '));
  await page.getByRole('button', { name: '停止', exact: true }).click();
  const stopped = await page.locator('#p').textContent();
  assert.equal((stopped.match(/日本語:/g) || []).length, 1);
  await page.waitForTimeout(350);
  assert.equal(await page.locator('#p').textContent(), stopped);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), original);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), stopped);
  await page.close();
});

test('Stop during balanced formatted-sentence recovery keeps completed sentences and restores original native nodes', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Round <strong>all</strong> symbols use <var>n</var>. Square <em>all</em> symbols use <var>m</var>.</p></div>', undefined, { markerLimit: 1, delay: 150 });
  const original = await page.locator('#p').textContent();
  await page.evaluate(() => { window.__retryOriginal = [...document.querySelector('#p').childNodes]; });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(request => request.payload).length === 3 && document.querySelector('#p').textContent.startsWith('日本語:'));
  await page.getByRole('button', { name: '停止', exact: true }).click();
  const stopped = await page.locator('#p').textContent();
  assert.match(stopped, /Square all symbols use m\.$/);
  await page.waitForTimeout(350);
  assert.equal(await page.locator('#p').textContent(), stopped);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), original);
  assert.equal(await page.locator('#p').evaluate(node => [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE).every((child, index) => child === window.__retryOriginal[index])), true);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), stopped);
  await page.close();
});

test('generated HTML is inserted as text', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Find the answer.</p></div>', undefined, { literalHtml: true });
  await translate(page);
  assert.equal(await page.locator('#p img').count(), 0);
  assert.match(await page.locator('#p').textContent(), /<img src=x onerror=alert\(1\)>/);
  await page.close();
});

test('a damaged paragraph stays original while later paragraphs continue', async () => {
  const page = await fixture('<div class="problem-statement"><p id="first">Find the answer <span class="katex">n</span>.</p><p id="second">Find the answer.</p></div>', undefined, { missingFirstMarker: true });
  await translate(page);
  assert.equal(await page.locator('#first').textContent(), 'Find the answer n.');
  assert.match(await page.locator('#second').textContent(), /日本語/);
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /1 箇所は失敗/);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 2);
  await page.close();
});

test('removed source-context preferences never send surrounding prose or translated history', async () => {
  const page = await fixture('<div class="problem-statement"><p>First paragraph <var>n</var>.</p><p>Second paragraph.</p></div>', undefined, { cached: 1, preferences: { 'cp-translator-use-source-context': true, 'cp-translator-use-history': true } });
  await translate(page);
  await chooseModel(page, '8b');
  await translate(page);
  await page.evaluate(() => { document.querySelector('.problem-statement').innerHTML = '<p>New problem paragraph.</p>'; });
  await translate(page);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload));
  assert.equal(requests.length, 5);
  assert.ok(requests.every(request => request.source_context === '' && request.history === undefined));
  assert.equal(requests[4].segments[0].text, 'New problem paragraph.');
  await page.close();
});

test('nested interactive controls retain their exact original nodes and listeners', async () => {
  const page = await fixture('<div class="problem-statement"><p id="widget">Find the answer <span><input id="input" value="state"><button id="action">Check</button></span>.</p><p id="plain">Find the answer.</p></div>');
  await page.evaluate(() => {
    window.__widget = document.querySelector('#widget');
    window.__input = document.querySelector('#input');
    document.querySelector('#input').value = 'edited value';
    document.querySelector('#action').addEventListener('click', () => { window.__clicked = true; });
  });
  await translate(page);
  assert.equal(await page.locator('#widget').textContent(), 'Find the answer Check.');
  assert.equal(await page.locator('#input').inputValue(), 'edited value');
  assert.equal(await page.evaluate(() => document.querySelector('#input') === window.__input && document.querySelector('#widget') === window.__widget), true);
  await page.locator('#action').click();
  assert.equal(await page.evaluate(() => window.__clicked), true);
  assert.match(await page.locator('#plain').textContent(), /日本語/);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 1);
  await page.close();
});

for (const mutation of ['text', 'subtree', 'insert', 'detach']) {
  test(`in-flight ${mutation} DOM update rejects the stale translation`, async () => {
    const page = await fixture('<div class="problem-statement"><p id="p">Find the answer <span class="katex">n</span>.</p></div>', undefined, { delay: 100 });
    await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
    await page.waitForFunction(() => window.__cpTranslatorRequests.some(request => request.payload));
    const expected = await page.evaluate(mutation => {
      const paragraph = document.querySelector('#p');
      window.__paragraph = paragraph;
      if (mutation === 'text') paragraph.firstChild.data = 'Updated statement ';
      else if (mutation === 'subtree') paragraph.querySelector('.katex').textContent = 'x + y';
      else if (mutation === 'insert') paragraph.insertBefore(document.createTextNode('new content'), paragraph.querySelector('.katex'));
      else paragraph.remove();
      return paragraph.outerHTML;
    }, mutation);
    await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('更新されました'));
    assert.equal(await page.evaluate(() => window.__paragraph.outerHTML), expected);
    await assertNoOriginalToggle(page);
    await page.close();
  });
}

test('stop aborts the current request and prevents late DOM changes', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Find the answer.</p><p>Another paragraph.</p></div>', undefined, { delay: 1000 });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.some(request => request.payload));
  await page.getByRole('button', { name: '停止', exact: true }).click();
  assert.equal(await page.locator('#p').textContent(), 'Find the answer.');
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /停止しました/);
  assert.equal(await page.getByRole('button', { name: '日本語に翻訳', exact: true }).isEnabled(), true);
  await page.close();
});

for (const url of ['https://www.codechef.com/problems/FLOW001', 'https://www.codechef.com/START32B/problems/FINDSHOES']) {
  test(`CodeChef: statement, KaTeX and samples survive on ${new URL(url).pathname}`, async () => {
    // #problem-statement and these wrapper shapes were verified against the
    // live, normally rendered FLOW001 page on 2026-11-05. CSS hashes are not selectors.
    const math = katex.renderToString(String.raw`0 \le A,B \le 10^4`, { output: 'htmlAndMathml' });
    const html = `<nav>Statement Hints Submissions Solution AI Help</nav><main><div id="problem-statement" class="_problemBody_bh3c4_71"><div class="_problemStatementWrapper_bh3c4_33"><h1 class="notranslate">Add Two Numbers</h1><div class="_problemBodyContent_bh3c4_71"><p id="p">You are given ${math} positive integers. Find the answer.</p><h3>Input Format</h3><p>Find the answer for each test case.</p><h3 class="notranslate">Sample 1:</h3><div><button id="copy">Copy</button><div class="_values_bh3c4_254"><pre id="input">3\n1 2\n100 200\n10 40</pre></div><div class="_values_bh3c4_254"><pre id="output">3\n300\n50</pre></div></div></div></div></div><aside id="editor">Code editor and Test against Custom Input</aside></main>`;
    const page = await fixture(html, url);
    const original = await page.locator('#problem-statement').textContent();
    const formulas = await page.locator('#problem-statement .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    const samples = await page.locator('pre').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    await translate(page);
    assert.equal(await page.locator('#cp-translator').evaluate(node => node.shadowRoot.querySelector('.site').textContent), 'CodeChef');
    assert.match(await page.locator('#p').textContent(), /日本語/);
    assert.deepEqual(await page.locator('#problem-statement .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML)), formulas);
    assert.deepEqual(await page.locator('pre').evaluateAll(nodes => nodes.map(node => node.outerHTML)), samples);
    assert.equal(await page.locator('#editor').textContent(), 'Code editor and Test against Custom Input');
    assert.equal(await page.locator('nav').textContent(), 'Statement Hints Submissions Solution AI Help');
    const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload.segments[0].text));
    assert.ok(sent.every(text => !text.includes('100 200') && !text.includes('Code editor') && !text.includes('AI Help')));
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#problem-statement').textContent(), original);
    await page.close();
  });
}

test('CodeChef: the first React problem can mount after browsing for more than 30 seconds', async () => {
  const page = await fixture('<main id="root"><p>Loading the statement</p></main>', 'https://www.codechef.com/', { clock: true });
  assert.equal(await page.locator('#cp-translator').count(), 0);
  await page.clock.fastForward(31000);
  await page.evaluate(() => {
    history.pushState({}, '', '/problems/FLOW001');
    const statement = document.createElement('div');
    statement.id = 'problem-statement';
    const paragraph = document.createElement('p');
    paragraph.id = 'p'; paragraph.textContent = 'Find the answer.';
    statement.append(paragraph); document.querySelector('main').replaceChildren(statement);
  });
  await translate(page);
  assert.match(await page.locator('#p').textContent(), /日本語/);
  await page.close();
});

test('CodeChef: React navigation reuses the panel and translates the newly mounted problem only', async () => {
  const page = await fixture('<main><div id="problem-statement"><p id="old">Find the answer.</p><pre>old sample</pre></div></main>', 'https://www.codechef.com/problems/FLOW001');
  await translate(page);
  await page.evaluate(() => {
    history.pushState({}, '', '/problems/CHEFPAROLE');
    const next = document.createElement('div'); next.id = 'problem-statement';
    const p = document.createElement('p'); p.id = 'next'; p.textContent = 'You are given positive integers. Find the answer.';
    const sample = document.createElement('pre'); sample.textContent = 'new sample\n2  3';
    next.append(p, sample); document.querySelector('#problem-statement').replaceWith(next);
  });
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#next').textContent(), 'You are given positive integers. Find the answer.');
  await translate(page);
  assert.equal(await page.locator('#cp-translator').count(), 1);
  assert.match(await page.locator('#next').textContent(), /日本語/);
  assert.equal(await page.locator('pre').textContent(), 'new sample\n2  3');
  assert.equal(await page.locator('#old').count(), 0);
  const last = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).at(-1).payload.segments[0].text);
  assert.match(last, /You are given positive integers/);
  await page.close();
});

for (const [name, url, html, target] of [
  ['CSES', 'https://cses.fi/problemset/task/1068/', '<div class="content"><ul class="task-constraints"><li>Time limit: 1 sec</li></ul><div class="md"><p id="p">Find the answer <span class="math">n</span>.</p><pre>1 2 3</pre></div></div>', '#p'],
  ['Kattis', 'https://open.kattis.com/problems/hello', '<div class="problembody"><p id="p">Find the answer.</p><table class="sample"><tr><td>Input</td></tr></table></div>', '#p'],
]) {
  test(`${name}: only the statement is translated`, async () => {
    const page = await fixture(html, url);
    await translate(page);
    assert.match(await page.locator(target).textContent(), /日本語/);
    if (name === 'CSES') assert.equal(await page.locator('.task-constraints').textContent(), 'Time limit: 1 sec');
    await page.close();
  });
}

test('general articles only activate through an explicit translation command', async () => {
  const page = await fixture('<nav>Navigation</nav><article><h1>Example article</h1><p id="p">Find the answer <mjx-container><svg aria-label="formula"></svg></mjx-container>.</p></article>', 'https://example.com/article');
  assert.equal(await page.locator('#cp-translator').count(), 0);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: このページの本文を翻訳']());
  await translate(page);
  assert.match(await page.locator('#p').textContent(), /日本語/);
  assert.equal(await page.locator('mjx-container svg').count(), 1);
  assert.equal(await page.locator('nav').textContent(), 'Navigation');
  assert.ok((await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload))).every(request => request.payload.document_type === 'general'));
  await page.close();
});

test('explicit whole-page translation starts for plain HTML without an article container', async () => {
  const page = await fixture('<nav>Navigation untouched</nav><p id="plain">Find the answer <var>n</var>.</p><input value="untouched"><pre>sample untouched</pre>', 'https://example.com/plain');
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']());
  assert.match(await page.locator('#plain').textContent(), /日本語/);
  assert.equal(await page.locator('nav').textContent(), 'Navigation untouched');
  assert.equal(await page.locator('pre').textContent(), 'sample untouched');
  assert.equal(await page.locator('input').inputValue(), 'untouched');
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload));
  assert.ok(requests.every(request => request.payload.document_type === 'general'));
  assert.equal(await page.locator('#cp-translator').count(), 1);
  await page.close();
});

test('general KaTeX articles send the general context and preserve rendered math and exact original text', async () => {
  const formula = katex.renderToString(String.raw`E = \frac{1}{2}mv^2`, { output: 'htmlAndMathml' });
  const display = katex.renderToString(String.raw`\int_a^b f(x)\,dx`, { displayMode: true, output: 'htmlAndMathml' });
  const page = await fixture(`<nav>Navigation</nav><article><h1>Energy and motion</h1><p id="general-text">The edge of the spectrum is described by ${formula}. Read <a href="/chapter">the next chapter</a>.</p><p>The area is ${display}.</p></article>`, 'https://example.com/physics');
  assert.equal(await page.locator('#cp-translator').count(), 0);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  const original = await page.locator('article').textContent();
  const math = await page.locator('.katex, .katex-display').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await page.evaluate(() => { window.__generalMathNodes = Array.from(document.querySelectorAll('.katex, .katex-display')); window.__cpTranslatorCommands['PLaMo: このページの本文を翻訳'](); });
  await translate(page);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload));
  assert.ok(requests.length > 0);
  assert.ok(requests.every(request => request.payload.document_type === 'general'));
  assert.ok(requests.every(request => !request.payload.segments[0].text.includes('\\frac')));
  assert.deepEqual(await page.locator('.katex, .katex-display').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  assert.equal(await page.locator('#general-text a').getAttribute('href'), '/chapter');
  assert.equal(await page.locator('nav').textContent(), 'Navigation');
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('article').textContent(), original);
  assert.equal(await page.evaluate(() => window.__generalMathNodes.every((node, index) => document.querySelectorAll('.katex, .katex-display')[index] === node)), true);
  await page.close();
});

test('arXiv HTML automatically shows its general-document toolbar and preserves published MathML, title, navigation and links', async () => {
  const article = await readFile(new URL('./fixtures/arxiv-html-math-paragraph.html', import.meta.url), 'utf8');
  const page = await fixture('<title>Original browser title</title><nav id="outside">Outside navigation.</nav>' + article, 'https://arxiv.org/html/2402.03300v3', { preferences: {
    'cp-translator-default-glossary': [{ source: 'jury', target: 'ジャッジ' }],
    'cp-translator-glossary': [{ source: 'jury', target: 'ジャッジ' }, { source: 'research', target: '研究' }],
  } });
  const original = await page.locator('article.ltx_document').textContent();
  const math = await page.locator('math').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  const href = await page.locator('article a').getAttribute('href');
  assert.equal(await page.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  await translate(page);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload));
  assert.ok(sent.length > 0 && sent.every(request => request.document_type === 'general' && request.source_lang === 'English'));
  assert.ok(sent.every(request => JSON.stringify(request.glossary) === JSON.stringify([{ source: 'research', target: '研究' }])));
  assert.equal(await page.title(), 'Original browser title');
  assert.equal(await page.locator('#outside').textContent(), 'Outside navigation.');
  assert.equal(await page.locator('#paper-navigation').textContent(), 'Paper navigation stays unchanged.');
  assert.match(await page.locator('#paper-title').textContent(), /^日本語/);
  assert.equal(await page.locator('article a').getAttribute('href'), href);
  assert.deepEqual(await page.locator('math').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('article.ltx_document').textContent(), original);
  await page.close();
});

test('connection errors explain opening the app without terminal instructions', async () => {
  const page = await fixture('<div class="problem-statement"><p>Find the answer.</p></div>', undefined, { networkError: true });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('接続できません'));
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /PLaMo for 競プロ アプリを開いて/);
  assert.doesNotMatch(await page.locator('#cp-translator').locator('.status').textContent(), /uv run|Python|127\.0\.0\.1/);
  await page.close();
});

test('automatic translation defaults off and honors a saved explicit model without changing it', async () => {
  const page = await fixture('<div class="problem-statement"><p>Find the answer.</p></div>', undefined, { preferences: { 'cp-translator-model': '8b' } });
  await page.waitForTimeout(180);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  assert.equal(await page.getByLabel('モデルサイズ').inputValue(), '8b');
  await page.close();
});

test('opt-in automatic translation runs once and ignores its own DOM changes and original toggles', async () => {
  const page = await fixture('<div class="problem-statement"><p id="p">Find the answer.</p></div>', undefined, { preferences: { 'cp-translator-auto-sites': { codeforces: true } } });
  await page.waitForFunction(() => document.querySelector('#p').textContent.startsWith('日本語'));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.evaluate(() => document.body.append(document.createElement('aside')));
  await page.waitForTimeout(250);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).length), 1);
  assert.equal(await page.locator('#p').textContent(), 'Find the answer.');
  assert.match(await page.locator('#cp-translator').locator('.panel').getAttribute('title'), /Built with PLaMo/);
  await page.close();
});

test('automatic CodeChef translation handles late statements and a new SPA document once each', async () => {
  const page = await fixture('<main></main>', 'https://www.codechef.com/', { preferences: { 'cp-translator-auto-sites': { codechef: true } } });
  await page.evaluate(() => { history.pushState({}, '', '/problems/FLOW001'); document.querySelector('main').innerHTML = '<div id="problem-statement"><p id="first-auto">Find the answer.</p></div>'; });
  await page.waitForFunction(() => document.querySelector('#first-auto').textContent.startsWith('日本語'));
  await page.evaluate(() => { history.pushState({}, '', '/problems/NEXT'); const next = document.createElement('div'); next.id = 'problem-statement'; const p = document.createElement('p'); p.id = 'next-auto'; p.textContent = 'You are given positive integers.'; next.append(p); document.querySelector('#problem-statement').replaceWith(next); });
  await page.waitForFunction(() => document.querySelector('#next-auto').textContent.startsWith('日本語'));
  await page.waitForTimeout(200);
  const sent = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload));
  assert.equal(sent.length, 2); assert.ok(sent.every(item => item.payload.document_type === 'problem'));
  assert.equal(sent[1].payload.history, undefined);
  assert.ok(!sent[1].payload.source_context.includes('Find the answer'));
  await page.close();
});

test('automatic custom articles use general context and leave navigation and code unchanged', async () => {
  const page = await fixture('<nav>Navigation</nav><article><p id="article-auto">The edge of this spectrum is visible.</p><pre>raw code</pre></article>', 'https://example.com/article', { preferences: { 'cp-translator-auto-sites': { article: true }, 'cp-translator-current-site': 'article', 'cp-translator-custom-site': true } });
  await page.waitForFunction(() => document.querySelector('#article-auto').textContent.startsWith('日本語'));
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.find(item => item.payload).payload.document_type), 'general');
  assert.equal(await page.locator('nav').textContent(), 'Navigation'); assert.equal(await page.locator('pre').textContent(), 'raw code');
  await page.close();
});

test('a common batch choice preserves failed paragraphs independently across model changes', async () => {
  const paragraphs = ['Find <var>x</var> first.', 'Second paragraph.', 'Third paragraph.', 'Fourth paragraph.', 'Fifth paragraph.'];
  const page = await fixture(`<div class="problem-statement">${paragraphs.map((html, index) => `<p id="batch-${index}">${html}</p>`).join('')}</div>`, undefined, { missingFirstMarker: true, errorIds: ['1'], preferences: { 'cp-translator-supported-batch-sizes': [1, 2, 4], 'cp-translator-model': '0.8b', 'cp-translator-batch-size': 4 } });
  const originals = await page.locator('#batch-0, #batch-1').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await translate(page);
  assert.deepEqual(await page.locator('#batch-0, #batch-1').evaluateAll(nodes => nodes.map(node => node.outerHTML)), originals);
  for (const index of [2, 3, 4]) assert.match(await page.locator(`#batch-${index}`).textContent(), /日本語/);
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /3 箇所を翻訳しました。2 箇所は失敗/);
  await chooseModel(page, '2b'); await translate(page);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.deepEqual(requests.map(item => item.segments.length), [4, 1, 4, 1]);
  assert.deepEqual(requests.map(item => item.batch_size), [4, 4, 4, 4]);
  assert.deepEqual(requests.slice(0, 2).flatMap(item => item.segments.map(segment => segment.id)), ['0', '1', '2', '3', '4']);
  assert.ok(requests.every(item => item.history === undefined && !item.source_context.includes('PLM_') && !item.source_context.includes('日本語')));
  await page.close();
});

test('eight-paragraph groups preserve each formula and skip failed segments before continuing the final group', async () => {
  const html = Array.from({ length: 9 }, (_, index) => `<p id="eight-${index}">Paragraph ${index} <var id="math-${index}">n_${index}</var>.</p>`).join('');
  const page = await fixture(`<div class="problem-statement">${html}<pre>1  2\n3</pre></div>`, undefined, { errorIds: ['3'], translations: { 6: 'マーカーが欠けた訳文' }, preferences: { 'cp-translator-model': '8b', 'cp-translator-batch-size': 8, 'cp-translator-supported-batch-sizes': [1, 2, 4, 8] } });
  const variables = await page.locator('var').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await translate(page);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.deepEqual(requests.map(item => item.segments.length), [8, 1]);
  assert.ok(requests.every(item => item.batch_size === 8));
  assert.deepEqual(requests.flatMap(item => item.segments.map(segment => segment.id)), Array.from({ length: 9 }, (_, index) => String(index)));
  assert.deepEqual(await page.locator('var').evaluateAll(nodes => nodes.map(node => node.outerHTML)), variables);
  for (const index of [3, 6]) assert.equal(await page.locator(`#eight-${index}`).textContent(), `Paragraph ${index} n_${index}.`);
  assert.match(await page.locator('#eight-8').textContent(), /日本語/);
  assert.match(await page.locator('#cp-translator').locator('.status').textContent(), /7 箇所を翻訳しました。2 箇所は失敗/);
  assert.equal(await page.locator('pre').textContent(), '1  2\n3');
  await page.close();
});

test('unsupported common batches fall back to sequential requests without changing the saved choice', async () => {
  const page = await fixture('<div class="problem-statement"><p>First paragraph.</p><p>Second paragraph.</p><p>Third paragraph.</p></div>', undefined, { preferences: { 'cp-translator-supported-batch-sizes': [1], 'cp-translator-batch-size': 16 } });
  await translate(page);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.equal(requests.length, 3); assert.ok(requests.every(item => item.segments.length === 1 && item.batch_size === 1));
  assert.equal(await page.evaluate(() => window.__cpTranslatorPreferences['cp-translator-batch-size']), 16);
  await page.close();
});

test('the page menu keeps known problem adapters and their problem context', async () => {
  const page = await fixture('<p id="outside">Outside problem text.</p><div class="problem-statement"><p id="p">Find the answer.</p><pre>1  2</pre></div>');
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']());
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.equal(requests.length, 1); assert.equal(requests[0].document_type, 'problem');
  assert.equal(requests[0].segments[0].text, 'Find the answer.');
  assert.equal(await page.locator('#outside').textContent(), 'Outside problem text.');
  assert.equal(await page.locator('pre').textContent(), '1  2');
  await page.close();
});

test('a missing common preference uses sixteen for every model and choices through32 apply to them all', async () => {
  const page = await fixture(`<div class="problem-statement">${Array.from({ length: 9 }, (_, index) => `<p>Paragraph ${index}.</p>`).join('')}</div>`, undefined, { preferences: { 'cp-translator-supported-batch-sizes': Array.from({ length: 32 }, (_, index) => index + 1) } });
  for (const model of ['0.8b', '2b', '8b']) { await chooseModel(page, model); await translate(page); }
  let requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.deepEqual(requests.map(item => item.segments.length), [9, 9, 9]);
  assert.ok(requests.every(item => item.batch_size === 16));
  await page.evaluate(() => { window.__cpTranslatorPreferences['cp-translator-batch-size'] = 3; window.__cpTranslatorRequests = []; });
  for (const model of ['0.8b', '2b', '8b']) { await chooseModel(page, model); await translate(page); }
  requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.ok(requests.every(item => item.batch_size === 3 && item.segments.length === 3));
  assert.equal(await page.evaluate(() => window.__cpTranslatorPreferences['cp-translator-batch-size']), 3);
  await page.evaluate(() => { window.__cpTranslatorPreferences['cp-translator-batch-size'] = 32; window.__cpTranslatorRequests = []; });
  for (const model of ['0.8b', '2b', '8b']) { await chooseModel(page, model); await translate(page); }
  requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.deepEqual(requests.map(item => item.segments.length), [9, 9, 9]);
  assert.ok(requests.every(item => item.batch_size === 32));
  assert.equal(await page.evaluate(() => window.__cpTranslatorPreferences['cp-translator-batch-size']), 32);
  await page.close();
});

test('automatic translation uses only the active site map and never the retired global switch', async () => {
  const page = await fixture('<div class="problem-statement"><p id="site-p">Find the answer.</p></div>', undefined, { preferences: { 'cp-translator-auto-translate': true, 'cp-translator-auto-sites': { codeforces: false, codechef: true } } });
  await page.waitForTimeout(180); assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  await page.evaluate(() => { window.__cpTranslatorPreferences['cp-translator-auto-sites'].codeforces = true; document.dispatchEvent(new Event('cp-translator-preferences-changed')); });
  await page.waitForFunction(() => document.querySelector('#site-p').textContent.startsWith('日本語'));
  await page.evaluate(() => {
    window.__cpTranslatorPreferences['cp-translator-disabled-sites'] = ['codeforces'];
    const next = document.createElement('div'); next.className = 'problem-statement'; next.textContent = 'Next problem paragraph.';
    history.pushState({}, '', '/problemset/problem/2/A'); document.querySelector('.problem-statement').replaceWith(next);
  });
  await page.waitForTimeout(200); assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).length), 1);
  await page.close();
});

test('automatic CodeChef translation retries the new statement when navigation reuses an in-flight root', async () => {
  const page = await fixture('<div id="problem-statement"><p>Old problem paragraph.</p></div>', 'https://www.codechef.com/problems/OLD', { delay: 200, preferences: { 'cp-translator-auto-sites': { codechef: true } } });
  await page.waitForFunction(() => window.__cpTranslatorRequests.some(item => item.payload));
  await page.evaluate(() => {
    window.__sameProblemRoot = document.querySelector('#problem-statement');
    history.pushState({}, '', '/problems/NEW'); const next = document.createElement('p'); next.id = 'new-problem'; next.textContent = 'New problem paragraph.';
    window.__sameProblemRoot.replaceChildren(next);
  });
  await page.waitForFunction(() => document.querySelector('#new-problem').textContent.startsWith('日本語'));
  await page.waitForTimeout(180);
  assert.equal(await page.evaluate(() => document.querySelector('#problem-statement') === window.__sameProblemRoot), true);
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.equal(requests.length, 2); assert.equal(requests[1].segments[0].text, 'New problem paragraph.');
  assert.ok(!requests[1].source_context.includes('Old problem'));
  await page.close();
});

test('original view can be selected before the first result and buffers later results without stale rejection', async () => {
  const page = await fixture('<div class="problem-statement"><p id="first">First paragraph <var id="formula">n</var>.</p><p id="second">Second paragraph.</p><p id="third">Third paragraph.</p></div>', undefined, { delay: 160 });
  await page.evaluate(() => { window.__sourceNodes = [...document.querySelector('#first').childNodes]; window.__sourceFormula = document.querySelector('#formula').outerHTML; });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 1);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 2);
  assert.equal(await page.locator('#first').textContent(), 'First paragraph n.');
  assert.equal(await page.evaluate(() => window.__sourceNodes.every((node, index) => document.querySelector('#first').childNodes[index + 1] === node)), true);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.match(await page.locator('#first').textContent(), /日本語/);
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 3);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('3 箇所を翻訳しました'));
  assert.deepEqual(await page.locator('p').allTextContents(), ['First paragraph n.', 'Second paragraph.', 'Third paragraph.']);
  assert.equal(await page.locator('#formula').evaluate(node => node.outerHTML), await page.evaluate(() => window.__sourceFormula));
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.ok((await page.locator('p').allTextContents()).every(text => text.startsWith('日本語')));
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__sourceNodes.every((node, index) => document.querySelector('#first').childNodes[index + 1] === node)), true);
  await page.close();
});

test('in-flight view switches preserve failed and stopped paragraphs while completed results remain switchable', async () => {
  const page = await fixture('<div class="problem-statement"><p id="first">First paragraph <var>n</var>.</p><p id="bad">Second paragraph <var>x</var>.</p><p id="pending">Third paragraph.</p></div>', undefined, { delay: 160, translations: { 1: '保護マーカーが欠けた訳文' } });
  const bad = await page.locator('#bad').evaluate(node => node.outerHTML);
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 2);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 3);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  await page.getByRole('button', { name: '停止', exact: true }).click();
  await page.waitForTimeout(200);
  assert.match(await page.locator('#first').textContent(), /日本語/);
  assert.equal(await page.locator('#bad').evaluate(node => node.outerHTML), bad);
  assert.equal(await page.locator('#pending').textContent(), 'Third paragraph.');
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#first').textContent(), 'First paragraph n.');
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.match(await page.locator('#first').textContent(), /日本語/);
  assert.equal(await page.locator('#bad').evaluate(node => node.outerHTML), bad);
  await page.close();
});

test('batch results are validated independently and remain buffered when original view is active', async () => {
  const page = await fixture('<div class="problem-statement"><p id="good">First paragraph <var>n</var>.</p><p id="bad">Second paragraph <var>x</var>.</p><p id="later">Third paragraph.</p></div>', undefined, { delay: 160, errorIds: ['1'], preferences: { 'cp-translator-model': '8b', 'cp-translator-batch-size': 8, 'cp-translator-supported-batch-sizes': [1, 2, 4, 8] } });
  const original = await page.locator('p').allTextContents();
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('2 箇所を翻訳しました。1 箇所は失敗'));
  assert.deepEqual(await page.locator('p').allTextContents(), original);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.match(await page.locator('#good').textContent(), /日本語/); assert.match(await page.locator('#later').textContent(), /日本語/);
  assert.equal(await page.locator('#bad').textContent(), original[1]);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).length), 1);
  await page.close();
});

test('genuine page mutations still reject in-flight output while original view is active', async () => {
  const page = await fixture('<div class="problem-statement"><p id="first">First paragraph.</p><p id="pending">Second paragraph <var>x</var>.</p></div>', undefined, { delay: 160 });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.filter(item => item.payload).length === 2);
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  await page.locator('#pending').evaluate(node => { node.firstChild.data = 'Updated paragraph '; });
  const updated = await page.locator('#pending').evaluate(node => node.outerHTML);
  await page.waitForFunction(() => document.querySelector('#cp-translator').shadowRoot.querySelector('.status').textContent.includes('更新されました'));
  assert.equal(await page.locator('#first').textContent(), 'First paragraph.');
  assert.equal(await page.locator('#pending').evaluate(node => node.outerHTML), updated);
  await page.getByRole('button', { name: '訳文を表示', exact: true }).click();
  assert.match(await page.locator('#first').textContent(), /日本語/);
  assert.equal(await page.locator('#pending').evaluate(node => node.outerHTML), updated);
  await page.close();
});

test('declared Chinese prose is collected without Latin letters while protected math and samples stay intact', async () => {
  const formula = katex.renderToString('a_i + b_i', { output: 'htmlAndMathml' });
  const page = await fixture(`<html lang="zh-CN"><body><article><p id="zh">给定一个长度为 ${formula} 的整数序列，请求出最大值。</p><p>输入格式</p><p>${formula}</p><pre>1  2\n3</pre></article></body></html>`, 'https://example.org/chinese');
  const math = await page.locator('.katex').evaluateAll(nodes => nodes.map(node => node.outerHTML));
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']());
  const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(item => item.payload).map(item => item.payload));
  assert.equal(requests.length, 2); assert.ok(requests.every(item => item.source_lang === 'Chinese' && item.document_type === 'general'));
  assert.ok(requests.some(item => item.segments[0].text === '输入格式'));
  assert.ok(requests.every(item => !item.source_context.includes('PLM_')));
  assert.deepEqual(await page.locator('.katex').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
  assert.equal(await page.locator('pre').textContent(), '1  2\n3');
  await page.close();
});

test('declared Indonesian documents send the explicit source language without changing English defaults', async () => {
  const page = await fixture('<html lang="id"><body><article><p>Diberikan sebuah bilangan bulat positif.</p></article></body></html>', 'https://example.org/indonesian');
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']());
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.find(item => item.payload).payload.source_lang), 'Indonesian');
  await page.close();
});

test('a configured toolbar suppression stays idle until an explicit manual translation', async () => {
  const page = await fixture('<article><p id="restricted">Restricted statement remains in its source language.</p></article>', 'https://policy.example/problem', { policyMessage: 'この問題は大会規定により翻訳できません。', preferences: { 'cp-translator-auto-sites': { 'policy-fixture': true } } });
  assert.equal(await page.locator('#cp-translator').count(), 0);
  await page.waitForTimeout(160);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
  await page.evaluate(async () => { await window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳'](); await window.__cpTranslatorCommands['PLaMo: このページの本文を翻訳'](); });
  await page.waitForTimeout(180);
  assert.equal(await page.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 1);
  assert.match(await page.locator('#restricted').textContent(), /^日本語/);
  await page.close();
});

for (const item of [
  { name: 'UOJ', url: 'https://uoj.ac/problem/1', sourceLanguage: 'Chinese', html: '<div id="tab-statement"><article class="uoj-article"><p id="p">给你两个数 $a$ 和 $b$，请输出他们的和。</p><h3>输入格式</h3><p>一行，两个用空格隔开的整数。</p><pre id="sample">2 3\n\n5</pre></article></div>' },
  { name: 'Project Euler', url: 'https://projecteuler.net/problem=1', sourceLanguage: 'English', html: '<div id="content"><div class="problem_content"><p id="p">Find the sum of all multiples of $3$ or $5$ below $1000$.</p><pre id="sample">3  5\n</pre></div></div>' },
  { name: 'Repovive', url: 'https://repovive.com/contests/30/problems/G', sourceLanguage: 'English', html: '<title>Adjacent Transformations - Repovive Premier Round 10 | Repovive</title><div class="prose"><p id="p">You are given two arrays <var id="v">A</var> and <var>B</var>.</p></div><pre id="sample">2  3\n\n4</pre><div class="prose problem-sample-markdown"><p>The arrays are already equal, so zero operations suffice.</p></div>' },
  { name: 'TLX', url: 'https://tlx.toki.id/courses/basic/chapters/implementation/problems/D', sourceLanguage: 'auto', html: '<html lang="en"><body><div class="programming-problem-statement__text"><div class="rich-statement-text"><div class="html-text"><p id="p">Diberikan barisan bilangan bulat <var id="v">A</var>.</p><p>Tentukan nilai terbesar.</p></div></div></div><pre id="sample">1  2\n3</pre></body></html>' },
]) {
  test(`${item.name}: verified statement selector and source language preserve formulas, samples and original text`, async () => {
    const page = await fixture(item.html + '<aside id="outside">Outside statement text.</aside>', item.url);
    const original = await page.locator('#p').textContent(), sample = await page.locator('#sample').evaluate(node => node.outerHTML);
    const variables = await page.locator('var').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    await translate(page);
    assert.match(await page.locator('#p').textContent(), /日本語/);
    const requests = await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).map(request => request.payload));
    assert.ok(requests.length > 0 && requests.every(request => request.source_lang === item.sourceLanguage && request.document_type === 'problem'));
    assert.ok(requests.every(request => !request.segments.some(segment => segment.text.includes('Outside statement'))));
    assert.equal(await page.locator('#sample').evaluate(node => node.outerHTML), sample);
    assert.deepEqual(await page.locator('var').evaluateAll(nodes => nodes.map(node => node.outerHTML)), variables);
    assert.equal(await page.locator('#outside').textContent(), 'Outside statement text.');
    await page.getByRole('button', { name: '原文を表示', exact: true }).click();
    assert.equal(await page.locator('#p').textContent(), original);
    await page.close();
  });
}

for (const item of [
  { contest: 'm1', title: 'Example - Repovive Premier Round 10 | Repovive', breadcrumb: 'Premier Round 10' },
  { contest: '1015', title: 'Gifts to the Right - Repovive Math Contest Round 1 | Repovive', breadcrumb: 'Round 1' },
  { contest: '1016', title: 'Example | Repovive', breadcrumb: 'Repovive Math Contest Round 2' },
]) {
  test(`Repovive MathRound suppresses its automatic toolbar but allows explicit page and selected-text translation for ${item.contest}`, async () => {
    const page = await fixture(`<title>${item.title}</title><a data-slot="breadcrumb-link" href="/contests/${item.contest}">${item.breadcrumb}</a><div class="prose"><p id="p">This contest problem must remain in the original language.</p></div>`, `https://repovive.com/contests/${item.contest}/problems/A`, { preferences: { 'cp-translator-custom-site': true, 'cp-translator-current-site': 'custom', 'cp-translator-auto-sites': { repovive: true, custom: true } } });
    assert.equal(await page.locator('#cp-translator').count(), 0);
    await page.waitForTimeout(160);
    assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.length), 0);
    await page.evaluate(async () => {
      await window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']();
      const selection = getSelection(); selection.selectAllChildren(document.querySelector('#p'));
      await window.__cpTranslatorCommands['PLaMo: 選択範囲を翻訳']();
    });
    await page.waitForTimeout(160);
    assert.equal(await page.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
    assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 2);
    assert.match(await page.locator('#p').textContent(), /^日本語/);
    await page.close();
  });
}

test('ordinary Repovive problems with math words in their own title remain translatable', async () => {
  const page = await fixture('<title>Math Contest Tricks - Example - Repovive Premier Round 10 | Repovive</title><a data-slot="breadcrumb-link" href="/contests/30">Repovive Premier Round 10</a><a data-slot="breadcrumb-link" href="/contests/30/problems/G">Math Contest Tricks</a><div class="prose"><p id="p">Find the answer.</p></div>', 'https://repovive.com/contests/30/problems/G');
  await translate(page); assert.match(await page.locator('#p').textContent(), /日本語/);
  await page.close();
});

test('TLX mounts its first HTML statement after a long SPA session without translating metadata', async () => {
  const page = await fixture('<main></main>', 'https://tlx.toki.id/', { clock: true });
  await page.clock.fastForward(31000);
  await page.evaluate(() => {
    history.pushState({}, '', '/courses/basic/chapters/implementation/problems/D');
    document.querySelector('main').innerHTML = '<aside id="outside">Metadata.</aside><div class="programming-problem-statement__text"><div class="html-text"><p id="p">Diberikan sebuah bilangan bulat.</p></div></div>';
  });
  await translate(page); assert.match(await page.locator('#p').textContent(), /日本語/);
  assert.equal(await page.locator('#outside').textContent(), 'Metadata.'); await page.close();
});

test('Repovive SPA navigation hides restricted toolbars, cancels in-flight translation and restores ordinary controls', async () => {
  const page = await fixture('<title>Gifts - Repovive Math Contest Round 1 | Repovive</title><main><div class="prose"><p>Blocked statement.</p></div></main>', 'https://repovive.com/contests/m1/problems/A', { delay: 400 });
  assert.equal(await page.locator('#cp-translator').count(), 0);
  await page.evaluate(() => { history.pushState({}, '', '/contests/30/problems/G'); document.title = 'Example - Repovive Premier Round 10 | Repovive'; document.querySelector('main').innerHTML = '<div class="prose"><p id="p">Ordinary problem paragraph.</p></div>'; });
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => window.__cpTranslatorRequests.some(request => request.payload));
  await page.evaluate(() => { history.pushState({}, '', '/contests/m2/problems/B'); document.title = 'Example - Repovive Math Contest Round 2 | Repovive'; document.querySelector('main').innerHTML = '<div class="prose"><p id="p">A new blocked statement.</p></div>'; });
  await page.waitForFunction(() => document.querySelector('#cp-translator').hidden);
  await page.waitForTimeout(450);
  assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.filter(request => request.payload).length), 1);
  assert.equal(await page.locator('#p').textContent(), 'A new blocked statement.');
  await page.evaluate(() => window.__cpTranslatorCommands['PLaMo: ページ全体を翻訳']());
  assert.match(await page.locator('#p').textContent(), /^日本語/);
  assert.equal(await page.getByRole('button', { name: '日本語に翻訳', exact: true }).isVisible(), true);
  await page.evaluate(() => { history.pushState({}, '', '/contests/m2/problems/C'); document.querySelector('main').innerHTML = '<div class="prose"><p id="p">Another problem in the same Math contest.</p></div>'; });
  await page.waitForFunction(() => document.querySelector('#cp-translator').hidden);
  assert.equal(await page.locator('#p').textContent(), 'Another problem in the same Math contest.');
  await page.evaluate(() => { history.pushState({}, '', '/contests/30/problems/H'); document.title = 'Example - Repovive Premier Round 10 | Repovive'; document.querySelector('main').innerHTML = '<div class="prose"><p id="p">Ordinary problem restored.</p></div>'; });
  await translate(page); assert.match(await page.locator('#p').textContent(), /日本語/);
  await page.close();
});

for (const item of [
  { name: 'UOJ', url: 'https://uoj.ac/problem/1', language: 'Chinese', html: '<div id="tab-statement"><article class="uoj-article"><p id="p">给定一个整数，请求出最大值。</p></article></div>' },
  { name: 'TLX', url: 'https://tlx.toki.id/courses/basic/chapters/implementation/problems/D', language: 'auto', html: '<html lang="en"><body><div class="programming-problem-statement__text"><div class="html-text"><p id="p">Diberikan sebuah bilangan bulat.</p></div></div></body></html>' },
]) {
  test(`${item.name}: selected prose within a problem retains its adapter language`, async () => {
    const page = await fixture(item.html, item.url);
    await page.evaluate(async () => { getSelection().selectAllChildren(document.querySelector('#p')); await window.__cpTranslatorCommands['PLaMo: 選択範囲を翻訳'](); });
    assert.match(await page.locator('#p').textContent(), /日本語/);
    assert.equal(await page.evaluate(() => window.__cpTranslatorRequests.find(request => request.payload).payload.source_lang), item.language);
    await page.close();
  });
}

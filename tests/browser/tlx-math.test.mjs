import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import katex from 'katex';

// Renderer structure captured from TLX G on 2026-11-07; all prose below is synthetic.
// Each of its six formulas has empty/rendered/empty KaTeX-only span siblings.
// https://tlx.toki.id/contests/compfest-18-scpc-penyisihan-mirror/problems/G
const tlxMarkupFixture = "<li><em>Adjust.</em> He ties a sample tag to item <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mi>u</mi></mrow><annotation encoding=\"application/x-tex\">u</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 0.43056em; vertical-align: 0em;\"></span><span class=\"mord mathnormal\">u</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span> and labels it with <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mo stretchy=\"false\">(</mo><mi>d</mi><mi>x</mi><mo separator=\"true\">,</mo><mi>d</mi><mi>y</mi><mo separator=\"true\">,</mo><mi>d</mi><mi>z</mi><mo stretchy=\"false\">)</mo></mrow><annotation encoding=\"application/x-tex\">(dx, dy, dz)</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 1em; vertical-align: -0.25em;\"></span><span class=\"mopen\">(</span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\">x</span><span class=\"mpunct\">,</span><span class=\"mspace\" style=\"margin-right: 0.166667em;\"></span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\" style=\"margin-right: 0.03588em;\">y</span><span class=\"mpunct\">,</span><span class=\"mspace\" style=\"margin-right: 0.166667em;\"></span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\" style=\"margin-right: 0.04398em;\">z</span><span class=\"mclose\">)</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span>. Because the entry associated with <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mi>u</mi></mrow><annotation encoding=\"application/x-tex\">u</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 0.43056em; vertical-align: 0em;\"></span><span class=\"mord mathnormal\">u</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span> is checked at the same time, <strong>each item in the collection of <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mi>u</mi></mrow><annotation encoding=\"application/x-tex\">u</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 0.43056em; vertical-align: 0em;\"></span><span class=\"mord mathnormal\">u</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span></strong>, including <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mi>u</mi></mrow><annotation encoding=\"application/x-tex\">u</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 0.43056em; vertical-align: 0em;\"></span><span class=\"mord mathnormal\">u</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span> as a special case, receives the common label <span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow><mo stretchy=\"false\">(</mo><mi>d</mi><mi>x</mi><mo separator=\"true\">,</mo><mi>d</mi><mi>y</mi><mo separator=\"true\">,</mo><mi>d</mi><mi>z</mi><mo stretchy=\"false\">)</mo></mrow><annotation encoding=\"application/x-tex\">(dx, dy, dz)</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"><span class=\"base\"><span class=\"strut\" style=\"height: 1em; vertical-align: -0.25em;\"></span><span class=\"mopen\">(</span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\">x</span><span class=\"mpunct\">,</span><span class=\"mspace\" style=\"margin-right: 0.166667em;\"></span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\" style=\"margin-right: 0.03588em;\">y</span><span class=\"mpunct\">,</span><span class=\"mspace\" style=\"margin-right: 0.166667em;\"></span><span class=\"mord mathnormal\">d</span><span class=\"mord mathnormal\" style=\"margin-right: 0.04398em;\">z</span><span class=\"mclose\">)</span></span></span></span></span><span><span class=\"katex\"><span class=\"katex-mathml\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><semantics><mrow></mrow><annotation encoding=\"application/x-tex\"></annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\"></span></span></span>.</li>";
const registry = (await readFile(new URL('../../browser/site-registry.js', import.meta.url), 'utf8')).replaceAll('export const ', 'const ').replaceAll('export function ', 'function ');
const source = await readFile(new URL('../../browser/translator.js', import.meta.url), 'utf8');
const url = 'https://tlx.toki.id/contests/compfest-18-scpc-penyisihan-mirror/problems/G';
let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser?.close(); });

const piece = tex => `<span>${katex.renderToString(tex)}</span>`;
const triple = tex => piece('') + piece(tex) + piece('');

async function fixture(contents, options = {}) {
  const page = await browser.newPage(); page.setDefaultTimeout(5000);
  await page.route('**/*', route => route.request().url() === url
    ? route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<!doctype html><html lang="en"><body><div class="programming-problem-statement__text"><div class="html-text">' + contents + '</div></div></body></html>' })
    : route.abort());
  await page.addInitScript(options => {
    window.__requests = [];
    window.readPreference = (key, fallback) => key === 'cp-translator-model' ? '2b' : fallback;
    window.writePreference = () => {};
    window.registerCommand = () => {};
    window.openManagement = () => {};
    window.requestHTTP = details => {
      const body = details.data ? JSON.parse(details.data) : null;
      window.__requests.push(body);
      const errors = body?.segments.filter(segment => options.rejectTweakGroups
        && segment.text.includes('Adjust.') && segment.text.includes('He ties a sample tag'))
        .map(segment => ({ id: segment.id, message: 'The protected math could not be retained.' })) || [];
      queueMicrotask(() => details.onload({ status: 200, responseText: JSON.stringify(body ? {
        translations: body.segments.filter(segment => !errors.some(error => error.id === segment.id))
          .map(segment => ({ id: segment.id, text: '日本語: ' + segment.text.replace('Adjust.', '調整。').replace('each item in the collection of', '集合内の各要素') })), errors, cached: 0,
      } : { status: 'ok', app: 'plamo-for-cp' }) }));
      return { abort() { details.onabort(); } };
    };
  }, options);
  await page.goto(url);
  await page.locator('#p').evaluate(node => { window.__originalChildren = [...node.childNodes]; });
  await page.evaluate(registry + '\n' + source);
  return page;
}

async function translate(page) {
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
  return page.evaluate(() => window.__requests.filter(Boolean).flatMap(request => request.segments.map(segment => segment.text)));
}

async function assertOriginalNodesRestored(page) {
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('#p').evaluate(node => {
    const children = [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE);
    return children.length === window.__originalChildren.length && children.every((child, index) => child === window.__originalChildren[index]);
  }), true);
}

test('TLX renderer structure keeps all eighteen renderer subtrees while sending six math slots and two real styles', async () => {
  const page = await fixture('<ul>' + tlxMarkupFixture.replace('<li>', '<li id="p">') + '</ul>');
  try {
    const originalMath = await page.locator('#p .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    assert.equal(originalMath.length, 18);
    const sent = await translate(page);
    assert.deepEqual(sent, ['<plm-a>Adjust.</plm-a> He ties a sample tag to item [[PLM_0]] and labels it with [[PLM_1]]. Because the entry associated with [[PLM_2]] is checked at the same time, <plm-b>each item in the collection of [[PLM_3]]</plm-b>, including [[PLM_4]] as a special case, receives the common label [[PLM_5]].']);
    assert.deepEqual(await page.locator('#p .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML)), originalMath);
    assert.equal(await page.locator('#p em').textContent(), '調整。');
    assert.match(await page.locator('#p strong').textContent(), /^集合内の各要素/);
    await assertOriginalNodesRestored(page);
  } finally { await page.close(); }
});

test('failed TLX-shaped synthetic groups retry whole sentences without cutting prose at either side of math', async () => {
  const page = await fixture('<ul>' + tlxMarkupFixture.replace('<li>', '<li id="p">') + '</ul>', { rejectTweakGroups: true });
  try {
    const math = await page.locator('#p .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    const sent = await translate(page);
    assert.equal(sent.length, 5, 'the whole paragraph and first group fail before its two terminal sentences');
    assert.equal(sent.filter(text => text.includes('Adjust.') && text.includes('He ties a sample tag')).length, 2);
    assert.ok(sent.includes('He ties a sample tag to item [[PLM_0]] and labels it with [[PLM_1]]. '));
    for (const text of sent.filter(text => text.includes('He ties'))) {
      assert.match(text, /He ties a sample tag to item \[\[PLM_0\]\] and labels it with \[\[PLM_1\]\]\./);
    }
    for (const text of sent.filter(text => text.includes('Because'))) {
      assert.match(text, /Because the entry associated with \[\[PLM_2\]\] is checked at the same time,/);
      assert.match(text, /<plm-b>each item in the collection of \[\[PLM_3\]\]<\/plm-b>, including \[\[PLM_4\]\] as a special case, receives the common label \[\[PLM_5\]\]\./);
    }
    const fragments = ['He ties a sample tag to item', 'and labels it with', '. Because the entry associated with',
      'is checked at the same time,', 'each item in the collection of', ', including', 'as a special case, receives the common label'];
    assert.ok(sent.every(text => !fragments.includes(text.trim())), 'formatting recovery must never submit math-adjacent clauses');
    assert.deepEqual(await page.locator('#p .katex').evaluateAll(nodes => nodes.map(node => node.outerHTML)), math);
    assert.match(await page.locator('#p strong').textContent(), /^集合内の各要素/);
    await assertOriginalNodesRestored(page);
  } finally { await page.close(); }
});

test('adjacent inline KaTeX wrappers and their whitespace stay one intact protected fragment', async () => {
  const page = await fixture('<p id="p">Use ' + triple('u') + ' \n\t ' + triple('v') + ' together.</p>');
  try {
    const originalMath = await page.locator('#p').innerHTML();
    const sent = await translate(page);
    assert.deepEqual(sent, ['Use [[PLM_0]] together.']);
    assert.equal(await page.locator('#p .katex').count(), 6);
    await assertOriginalNodesRestored(page);
    assert.equal((await page.locator('#p').innerHTML()).replaceAll('<!--cp-translator-start-->', '').replaceAll('<!--cp-translator-end-->', ''), originalMath);
  } finally { await page.close(); }
});

test('prose and link spans remain translatable boundaries between independent math fragments', async () => {
  const page = await fixture('<p id="p">Use ' + triple('u') + '<a href="/help" title="Keep link">Read ' + triple('v') + ' carefully</a>' + triple('w') + '<span class="prose">' + triple('x') + ' apples</span>' + triple('y') + ' now.</p>');
  try {
    const sent = await translate(page);
    assert.deepEqual(sent, ['Use [[PLM_0]]<plm-a>Read [[PLM_1]] carefully</plm-a>[[PLM_2]]<plm-b>[[PLM_3]] apples</plm-b>[[PLM_4]] now.']);
    assert.equal(await page.locator('#p a').getAttribute('href'), '/help');
    assert.equal(await page.locator('#p a').getAttribute('title'), 'Keep link');
    assert.match(await page.locator('#p .prose').textContent(), /apples$/);
    assert.equal(await page.locator('#p .katex').count(), 15);
    await assertOriginalNodesRestored(page);
  } finally { await page.close(); }
});

test('display math and block wrappers are never swallowed into the adjacent inline group', async () => {
  const display = katex.renderToString('a+b', { displayMode: true });
  const page = await fixture('<p id="p">Use ' + triple('u') + display + triple('v') + '<span style="display:block">' + piece('w') + '</span> now.</p>');
  try {
    const before = await page.locator('#p .katex-display').evaluate(node => node.outerHTML);
    const sent = await translate(page);
    assert.deepEqual(sent, ['Use [[PLM_0]][[PLM_1]][[PLM_2]]<plm-a>[[PLM_3]]</plm-a> now.']);
    assert.equal(await page.locator('#p .katex-display').evaluate(node => node.outerHTML), before);
    await assertOriginalNodesRestored(page);
  } finally { await page.close(); }
});

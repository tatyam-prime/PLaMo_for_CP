import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';

// Renderer structure captured 2026-11-07; prose is synthetic. P + display MathJax span + continuation P.
// https://open.kattis.com/contests/nacentral26d2open/problems/fibonacho
const kattisMarkupFixture = "<!doctype html><html lang=en><body><div class=problembody><p>A teacher defines a sample sequence as a <em>demo\n    pattern</em> when it follows</p>\n<span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" display=\"true\" tabindex=\"0\" ctxtmenu_counter=\"0\" style=\"font-size: 101.6%; position: relative;\"><mjx-math display=\"true\" class=\"MJX-TEX\" aria-hidden=\"true\" style=\"margin-left: 0px; margin-right: 0px;\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mi class=\"mjx-i\" size=\"s\"><mjx-c class=\"mjx-c1D456 TEX-I\"></mjx-c></mjx-mi></mjx-script></mjx-msub><mjx-mo class=\"mjx-n\" space=\"4\"><mjx-c class=\"mjx-c3D\"></mjx-c></mjx-mo><mjx-msub space=\"4\"><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-texatom size=\"s\" texclass=\"ORD\"><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D456 TEX-I\"></mjx-c></mjx-mi><mjx-mo class=\"mjx-n\"><mjx-c class=\"mjx-c2212\"></mjx-c></mjx-mo><mjx-mn class=\"mjx-n\"><mjx-c class=\"mjx-c33\"></mjx-c></mjx-mn></mjx-texatom></mjx-script></mjx-msub><mjx-mo class=\"mjx-n\" space=\"3\"><mjx-c class=\"mjx-c2B\"></mjx-c></mjx-mo><mjx-msub space=\"3\"><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-texatom size=\"s\" texclass=\"ORD\"><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D456 TEX-I\"></mjx-c></mjx-mi><mjx-mo class=\"mjx-n\"><mjx-c class=\"mjx-c2212\"></mjx-c></mjx-mo><mjx-mn class=\"mjx-n\"><mjx-c class=\"mjx-c31\"></mjx-c></mjx-mn></mjx-texatom></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"block\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\" display=\"block\"><msub><mi>a</mi><mi>i</mi></msub><mo>=</mo><msub><mi>a</mi><mrow data-mjx-texclass=\"ORD\"><mi>i</mi><mo>−</mo><mn>3</mn></mrow></msub><mo>+</mo><msub><mi>a</mi><mrow data-mjx-texclass=\"ORD\"><mi>i</mi><mo>−</mo><mn>1</mn></mrow></msub></math></mjx-assistive-mml></mjx-container></span>\n<p>for every <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"1\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D456 TEX-I\"></mjx-c></mjx-mi><mjx-mo class=\"mjx-n\" space=\"4\"><mjx-c class=\"mjx-c2265\"></mjx-c></mjx-mo><mjx-mn class=\"mjx-n\" space=\"4\"><mjx-c class=\"mjx-c33\"></mjx-c></mjx-mn></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><mi>i</mi><mo>≥</mo><mn>3</mn></math></mjx-assistive-mml></mjx-container></span>.\n    With <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"2\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c33\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>3</mn></msub></math></mjx-assistive-mml></mjx-container></span>, <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"3\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c34\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>4</mn></msub></math></mjx-assistive-mml></mjx-container></span>, and <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"4\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c35\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>5</mn></msub></math></mjx-assistive-mml></mjx-container></span>, calculate <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"5\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c30\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>0</mn></msub></math></mjx-assistive-mml></mjx-container></span>, <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"6\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c31\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>1</mn></msub></math></mjx-assistive-mml></mjx-container></span>, and <span class=\"tex2jax_process\"><mjx-container class=\"MathJax CtxtMenu_Attached_0\" jax=\"CHTML\" tabindex=\"0\" ctxtmenu_counter=\"7\" style=\"font-size: 101.6%; position: relative;\"><mjx-math class=\"MJX-TEX\" aria-hidden=\"true\"><mjx-msub><mjx-mi class=\"mjx-i\"><mjx-c class=\"mjx-c1D44E TEX-I\"></mjx-c></mjx-mi><mjx-script style=\"vertical-align: -0.15em;\"><mjx-mn class=\"mjx-n\" size=\"s\"><mjx-c class=\"mjx-c32\"></mjx-c></mjx-mn></mjx-script></mjx-msub></mjx-math><mjx-assistive-mml unselectable=\"on\" display=\"inline\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\"><msub><mi>a</mi><mn>2</mn></msub></math></mjx-assistive-mml></mjx-container></span>. For this example, the\n    three missing entries are fixed by the displayed rule and may\n    have either sign.</p></div></body></html>";
const registry = (await readFile(new URL('../../browser/site-registry.js', import.meta.url), 'utf8')).replaceAll('export const ', 'const ').replaceAll('export function ', 'function ');
const source = await readFile(new URL('../../browser/translator.js', import.meta.url), 'utf8');
const address = 'https://open.kattis.com/contests/nacentral26d2open/problems/fibonacho';
const display = '<span class="tex2jax_process"><mjx-container class="MathJax" display="true"><mjx-math aria-hidden="true">x = y + z</mjx-math></mjx-container></span>';
let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser?.close(); });

async function fixture(contents, options = {}) {
  const page = await browser.newPage(); page.setDefaultTimeout(5000);
  const url = options.url || address;
  const html = options.actual ? contents : '<!doctype html><html lang="en"><body><div class="problembody problem-statement">' + contents + '</div></body></html>';
  await page.route('**/*', route => route.request().url() === url ? route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }) : route.abort());
  await page.addInitScript(({ preferences }) => {
    window.__requests = [];
    window.readPreference = (key, fallback) => preferences[key] ?? fallback;
    window.writePreference = () => {};
    window.registerCommand = () => {};
    window.openManagement = () => {};
    window.requestHTTP = details => {
      const body = details.data ? JSON.parse(details.data) : null;
      window.__requests.push(body);
      queueMicrotask(() => details.onload({ status: 200, responseText: JSON.stringify(body ? {
        translations: body.segments.map(segment => ({ id: segment.id, text: segment.text.replace('A teacher', '教師').replace('for every', 'すべての') })), cached: 0,
      } : { status: 'ok', app: 'plamo-for-cp' }) }));
      return { abort() { details.onabort(); } };
    };
  }, { preferences: options.preferences || {} });
  await page.goto(url);
  await page.locator('.problembody').evaluate(node => { window.__originalChildren = [...node.childNodes]; });
  await page.evaluate(registry + '\n' + source);
  return page;
}

async function translate(page) {
  await page.getByRole('button', { name: '日本語に翻訳', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#cp-translator')?.shadowRoot.querySelector('.status').textContent.includes('箇所を翻訳しました'));
  return page.evaluate(() => window.__requests.filter(Boolean).flatMap(request => request.segments.map(segment => segment.text)));
}

async function assertOriginal(page) {
  await page.getByRole('button', { name: '原文を表示', exact: true }).click();
  assert.equal(await page.locator('.problembody').evaluate(node => {
    const children = [...node.childNodes].filter(child => child.nodeType !== Node.COMMENT_NODE);
    return children.length === window.__originalChildren.length && children.every((child, index) => child === window.__originalChildren[index]);
  }), true);
}

test('Kattis-shaped synthetic definition includes its display equation and continuation in one input while restoring exact layout nodes', async () => {
  const page = await fixture(kattisMarkupFixture, { actual: true });
  try {
    const originalMath = await page.locator('mjx-container').evaluateAll(nodes => nodes.map(node => node.outerHTML));
    await page.locator('.problembody > p').first().evaluate(node => node.addEventListener('original-test', () => { window.__originalEvent = true; }));
    const sent = await translate(page);
    assert.equal(sent.length, 1);
    assert.match(sent[0], /A teacher.*when it follows<\/plm-a>\s*<plm-c>\[\[PLM_0\]\]<\/plm-c>\s*<plm-d>for every/s);
    assert.equal((sent[0].match(/\[\[PLM_\d+\]\]/g) || []).length, 8);
    assert.equal(await page.locator('.problembody > p').count(), 2);
    assert.match(await page.locator('.problembody > p').first().textContent(), /^教師/);
    assert.deepEqual(await page.locator('mjx-container').evaluateAll(nodes => nodes.map(node => node.outerHTML)), originalMath);
    await assertOriginal(page);
    await page.locator('.problembody > p').first().dispatchEvent('original-test');
    assert.equal(await page.evaluate(() => window.__originalEvent), true);
  } finally { await page.close(); }
});

for (const item of [
  { name: 'complete preceding sentence', before: 'The definition requires.', between: display, after: 'for every positive integer.' },
  { name: 'independent capitalized paragraph', before: 'The definition requires', between: display, after: 'Another independent paragraph follows.' },
  { name: 'heading boundary', before: 'The definition requires', between: display + '<h2>Input</h2>', after: 'for every positive integer.' },
  { name: 'prose inside a display wrapper', before: 'The definition requires', between: '<span>Some explanatory prose ' + display + '</span>', after: 'for every positive integer.' },
  { name: 'link inside a display wrapper', before: 'The definition requires', between: '<span><a href="/help">' + display + '</a></span>', after: 'for every positive integer.' },
  { name: 'interactive display wrapper', before: 'The definition requires', between: '<span>' + display + '<button>Widget</button></span>', after: 'for every positive integer.' },
  { name: 'table boundary', before: 'The definition requires', between: display + '<table><tr><td>Separate data</td></tr></table>', after: 'for every positive integer.' },
  { name: 'too many display wrappers', before: 'The definition requires', between: display.repeat(9), after: 'for every positive integer.' },
  { name: 'unbounded paragraph context', before: 'The lengthy definition requires '.repeat(30), between: display, after: 'for every positive integer.' },
  { name: 'other judge adapter', before: 'The definition requires', between: display, after: 'for every positive integer.', url: 'https://codeforces.com/problemset/problem/1/A' },
]) {
  test('display context grouping does not cross ' + item.name, async () => {
    const page = await fixture('<p id="first">' + item.before + '</p>\n' + item.between + '\n<p id="last">' + item.after + '</p>', { url: item.url });
    try {
      const sent = await translate(page);
      assert.ok(sent.some(text => text.includes(item.before.trim())));
      assert.ok(sent.some(text => text.includes(item.after)));
      assert.ok(!sent.some(text => text.includes(item.before.trim()) && text.includes(item.after)));
      await assertOriginal(page);
    } finally { await page.close(); }
  });
}

test('editable paragraphs are never pulled into a display-context record', async () => {
  const page = await fixture('<p contenteditable="true">The editable definition requires</p>' + display + '<p>for every positive integer.</p>');
  try {
    const original = await page.locator('[contenteditable]').evaluate(node => node.outerHTML);
    const sent = await translate(page);
    assert.ok(!sent.some(text => text.includes('editable definition')));
    assert.equal(await page.locator('[contenteditable]').evaluate(node => node.outerHTML), original);
    await assertOriginal(page);
  } finally { await page.close(); }
});

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';

let browser;
before(async () => { browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true }); });
after(async () => { await browser?.close(); });

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function until(check) { const deadline = Date.now() + 5000; while (!check()) { if (Date.now() > deadline) throw new Error('Timed out waiting for mock job state'); await wait(10); } }
const requests = control => control.requests.filter(request => request.path === '/api/translation-jobs');
const allSegments = control => requests(control).flatMap(request => request.body.segments);
const paragraphs = count => Array.from({ length: count }, (_, index) => `English paragraph number ${index + 1}.`).join('\n\n');
const defaultGlossary = [
  { source: 'modular inverse', target: '逆元' }, { source: 'proper prefix', target: '真の接頭辞' },
  { source: 'parallel edge', target: '多重辺' }, { source: 'prefix sum', target: '累積和' }, { source: 'jury', target: 'ジャッジ' },
  { source: 'subarray', target: '連続部分配列' }, { source: 'increment', target: 'インクリメント' },
  { source: 'pairwise disjoint', target: '互いに交差しない' },
  { source: 'sequence', target: '列' },
  { source: 'visualizer', target: 'ビジュアライザ' },
  { source: 'forest', target: '森' },
];

async function fixture() {
  const page = await browser.newPage(); page.setDefaultTimeout(5000);
  const control = {
    requests: [], jobs: new Map(), unexpected: [], manual: false, holdCreations: false, pendingCreations: [],
    errors: new Set(), previews: new Map(), output: segment => '訳文: ' + segment.text,
    setup: { application: 'plamo-for-cp', selected_model: '2b', supported_translation_jobs: true,
      default_glossary: defaultGlossary, supported_translation_previews: true,
      supported_batch_sizes: Array.from({ length: 16 }, (_, index) => index + 1),
      models: ['0.8b', '2b', '8b'].map(id => ({ id, available: true, compatible: true })) },
  };
  const assets = new Map(await Promise.all(['text.html', 'text.js', 'translation-jobs.js'].map(async name => [name, await readFile(new URL('../../plamo_cp/assets/' + name, import.meta.url))])));
  await page.addInitScript(() => {
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copied.push(text); } } });
  });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== 'http://127.0.0.1:8765') { control.unexpected.push(request.url()); return route.fulfill({ status: 404 }); }
    if (path === '/text') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: assets.get('text.html') });
    if (path === '/text.js' || path === '/translation-jobs.js') return route.fulfill({ contentType: 'application/javascript; charset=utf-8', body: assets.get(path.slice(1)) });
    if (path === '/favicon.ico') return route.fulfill({ status: 204 });
    if (!path.startsWith('/api/')) { control.unexpected.push(request.url()); return route.fulfill({ status: 404 }); }
    const record = { path, at: Date.now(), method: request.method(), headers: request.headers(), body: request.postData() ? request.postDataJSON() : null };
    control.requests.push(record);
    if (path === '/api/setup') return route.fulfill({ json: control.setup });
    if (path === '/api/translation-jobs') {
      const id = `00000000-0000-4000-8000-${String(control.jobs.size + 1).padStart(12, '0')}`;
      const events = record.body.segments.map((segment, index) => ({ ready: !control.manual, seq: control.manual ? null : index + 1, event: control.errors.has(segment.id)
        ? { type: 'segment_error', id: segment.id, message: '数式を保持できませんでした。' }
        : { type: 'translation', id: segment.id, text: control.output(segment), cached: false } }));
      control.jobs.set(id, { id, events, cancelled: false, sequence: control.manual ? 0 : events.length });
      if (control.holdCreations) await new Promise(resolve => control.pendingCreations.push(resolve));
      try { return await route.fulfill({ status: 202, json: { id, state: 'queued', cursor: 0, total: events.length } }); } catch { return; }
    }
    const match = /^\/api\/translation-jobs\/([\da-f-]+)(\/cancel)?$/.exec(path);
    if (match) {
      const id = match[1], job = control.jobs.get(id);
      if (!job) return route.fulfill({ status: 404, json: {} });
      if (match[2]) { job.cancelled = true; return route.fulfill({ json: { id, state: 'cancelled' } }); }
      const events = job.events.filter(item => item.ready).sort((left, right) => left.seq - right.seq).map(item => ({ seq: item.seq, ...item.event }));
      const after = Number(url.searchParams.get('after') || 0);
      const previews = [...control.previews.values()].filter(preview => !job.cancelled && job.events.some(item => item.event.id === preview.id && !item.ready));
      return route.fulfill({ json: { id, state: job.cancelled ? 'cancelled' : events.length === job.events.length ? 'completed' : 'running', cursor: events.length, events: events.filter(event => event.seq > after), previews, completed: events.length, total: job.events.length, cached: 0 } });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  control.releaseCreations = () => { for (const resolve of control.pendingCreations.splice(0)) resolve(); };
  control.complete = id => { for (const job of control.jobs.values()) for (const item of job.events) if (item.event.id === id && !item.ready) { item.ready = true; item.seq = ++job.sequence; } };
  control.finishAll = () => { control.manual = false; for (const job of control.jobs.values()) for (const item of job.events) if (!item.ready) { item.ready = true; item.seq = ++job.sequence; } };
  await page.goto('http://127.0.0.1:8765/text');
  await page.waitForFunction(() => !document.querySelector('#model').disabled);
  return { page, control };
}
async function finished(page) { await page.waitForFunction(() => document.querySelector('#status').textContent.includes('段落を翻訳しました。')); }
async function prefetch(page, number) { await page.locator('#prefetch').fill(String(number)); await page.locator('#prefetch').press('Tab'); }

test('the two-pane page translates pasted text explicitly without PDF, remote fetch, model download, or file download', async () => {
  const { page, control } = await fixture(); let downloads = 0; page.on('download', () => downloads++);
  assert.equal(control.requests.length, 1);
  assert.equal(await page.locator('#source').count(), 1); assert.equal(await page.locator('#results').count(), 1);
  assert.equal(await page.locator('input[type=file],iframe').count(), 0);
  const source = 'You are given two positive integers.\n\nPrint their sum.';
  await page.locator('#source').fill(source);
  assert.equal(requests(control).length, 0);
  control.output = segment => segment.text.startsWith('You') ? '2つの正の整数が与えられる。' : 'それらの和を出力せよ。';
  await page.locator('#translate').click(); await finished(page);
  assert.equal(await page.locator('#source').inputValue(), source);
  assert.equal(await page.locator('.translation').count(), 2);
  assert.equal(requests(control)[0].headers['x-plamo-client'], 'userscript');
  assert.equal(requests(control)[0].body.model, '2b'); assert.equal(requests(control)[0].body.batch_size, 4);
  assert.equal(requests(control)[0].body.document_type, 'problem');
  assert.deepEqual(requests(control)[0].body.glossary, defaultGlossary);
  assert.equal(requests(control)[0].body.source_context, '');
  assert.equal(downloads, 0); assert.deepEqual(control.unexpected, []);
  assert.ok(control.requests.every(request => !/pdf|fetch|prepare|download/.test(request.path)));
  await page.screenshot({ path: '.local/text-workspace.png', fullPage: true });
  await page.close();
});

test('requests ramp up without waiting for completion and keep exactly N unfinished segments', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#model').selectOption('2b'); await prefetch(page, 4);
  await page.locator('#source').fill(paragraphs(8)); await page.locator('#translate').click();
  await until(() => allSegments(control).length === 4);
  assert.deepEqual(requests(control).map(request => request.body.segments.length), [1, 1, 1, 1]);
  assert.ok(requests(control)[3].at - requests(control)[0].at >= 220, 'initial requests should be staggered');
  assert.equal(await page.locator('.translation').count(), 0);
  control.complete('text-2');
  await page.waitForFunction(() => document.querySelectorAll('.translation').length === 1);
  await until(() => allSegments(control).length === 5);
  assert.equal([...control.jobs.values()].filter(job => !job.events.some(item => item.ready)).length, 4);
  assert.equal(await page.locator('#stop').isVisible(), true);
  assert.ok(requests(control).every(request => request.body.batch_size === 4));
  control.finishAll(); await finished(page);
  assert.equal(await page.locator('.translation').count(), 8);
  await page.close();
});

test('raw TeX and fenced code retain exact bytes and never enter reference context as prose', async () => {
  const { page, control } = await fixture();
  const source = 'The formula is \\(a^2+b^2\\), $$$n+1$$$ and $x$.\n\n$$$$$$\n\\sum_{i=1}^{n} i\n$$$$$$\n\n```cpp\nint x = 1;\n\nreturn x;\n```\n\nFind the answer.';
  await page.locator('#source').fill(source); await page.locator('#translate').click(); await finished(page);
  const sent = allSegments(control).map(segment => segment.text).join('\n');
  assert.ok(sent.includes('[[PLM_0]]')); assert.ok(!sent.includes('a^2+b^2'));
  assert.ok(!sent.includes('int x = 1') && !sent.includes('sum_{i'));
  assert.ok(requests(control).every(request => !request.body.source_context.includes('int x = 1') && !request.body.source_context.includes('a^2+b^2')));
  const output = await page.locator('#results').textContent();
  for (const exact of ['\\(a^2+b^2\\)', '$$$n+1$$$', '$x$', '$$$$$$\n\\sum_{i=1}^{n} i\n$$$$$$', '```cpp\nint x = 1;\n\nreturn x;\n```']) assert.ok(output.includes(exact), exact);
  assert.equal(requests(control).length, 2, 'formula-only/code-only paragraphs need no inference');
  await page.close();
});

test('per-segment errors retain original text and successful output is rendered as text, never HTML', async () => {
  const { page, control } = await fixture();
  const failed = 'An original paragraph with $n+1$.';
  control.errors.add('text-0'); control.output = () => '<img src=x onerror="window.__xss=true"> 正しい訳文';
  await page.locator('#source').fill(failed + '\n\nA successful paragraph.');
  await page.locator('#translate').click(); await finished(page);
  assert.equal(await page.locator('.failed').textContent(), failed);
  assert.equal(await page.locator('.translation').textContent(), '<img src=x onerror="window.__xss=true"> 正しい訳文');
  assert.equal(await page.locator('#results img').count(), 0); assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.match(await page.locator('#status').textContent(), /1段落は原文/);
  await page.close();
});

test('Stop retains completed paragraphs and cancels known unfinished jobs', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#source').fill(paragraphs(4)); await page.locator('#translate').click();
  await until(() => control.jobs.size === 1); control.complete('text-0');
  await page.waitForFunction(() => document.querySelectorAll('.translation').length === 1);
  await until(() => control.jobs.size >= 2);
  await page.locator('#stop').click();
  await until(() => [...control.jobs.values()].some(job => job.cancelled));
  control.finishAll(); await page.waitForTimeout(250);
  assert.equal(await page.locator('.translation').count(), 1);
  assert.match(await page.locator('#status').textContent(), /停止しました/);
  assert.equal(await page.locator('#translate').isEnabled(), true);
  await page.close();
});

test('editing during delayed creation cancels its late ID and never applies results to the new source', async () => {
  const { page, control } = await fixture(); control.holdCreations = true;
  await page.locator('#source').fill('The old source.'); await page.locator('#translate').click();
  await until(() => control.jobs.size === 1);
  await page.locator('#source').fill('A new source.');
  control.releaseCreations();
  await until(() => [...control.jobs.values()][0].cancelled);
  assert.equal(await page.locator('.translation').count(), 0);
  assert.equal(await page.locator('#source').inputValue(), 'A new source.');
  control.holdCreations = false; await page.locator('#translate').click(); await finished(page);
  assert.equal(await page.locator('.translation').textContent(), '訳文: A new source.');
  await page.locator('#clear').click();
  assert.equal(await page.locator('#source').inputValue(), ''); assert.equal(await page.locator('.translation').count(), 0);
  assert.equal(await page.locator('#copy').isDisabled(), true);
  await page.close();
});

test('copy uses source order even when completions arrive out of order', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#source').fill(paragraphs(3)); await page.locator('#translate').click();
  await until(() => control.jobs.size === 1); control.complete('text-0');
  await until(() => allSegments(control).length === 3); control.complete('text-2');
  await page.waitForFunction(() => document.querySelectorAll('.translation').length === 2);
  control.complete('text-1'); await finished(page); await page.locator('#copy').click();
  assert.deepEqual(await page.evaluate(() => window.__copied), ['訳文: English paragraph number 1.\n\n訳文: English paragraph number 2.\n\n訳文: English paragraph number 3.']);
  await page.close();
});

test('per-model integer preferences1..16 persist, storage reset restores defaults, and focus keeps an explicit model', async () => {
  const { page, control } = await fixture();
  await page.locator('#model').selectOption('2b'); await prefetch(page, 16);
  await page.locator('#model').selectOption('0.8b'); assert.equal(await page.locator('#prefetch').inputValue(), '4');
  await page.locator('#model').selectOption('2b'); assert.equal(await page.locator('#prefetch').inputValue(), '16');
  await prefetch(page, 17); assert.equal(await page.locator('#prefetch').inputValue(), '16');
  await page.reload(); await page.waitForFunction(() => !document.querySelector('#model').disabled);
  await page.locator('#model').selectOption('2b'); assert.equal(await page.locator('#prefetch').inputValue(), '16');
  control.setup.selected_model = '8b'; await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForTimeout(30); assert.equal(await page.locator('#model').inputValue(), '2b');
  await page.evaluate(() => { localStorage.removeItem('plamo-text-prefetch'); window.dispatchEvent(new StorageEvent('storage', { key: 'plamo-text-prefetch', newValue: null })); });
  assert.equal(await page.locator('#prefetch').inputValue(), '4');
  await page.locator('#model').selectOption('8b'); assert.equal(await page.locator('#prefetch').inputValue(), '8');
  await prefetch(page, 1); await page.locator('#source').fill('A paragraph.'); await page.locator('#translate').click(); await finished(page);
  assert.equal(requests(control).at(-1).body.batch_size, 1);
  await page.close();
});

test('device capability, selected language and document kind reach jobs without preparing missing models', async () => {
  const { page, control } = await fixture();
  control.setup.supported_batch_sizes = [1];
  await page.evaluate(() => window.dispatchEvent(new Event('focus'))); await page.waitForTimeout(30);
  await page.locator('#source-language').selectOption('Chinese'); await page.locator('#document-type').selectOption('general');
  await page.locator('#source').fill('给定一个正整数。'); await page.locator('#translate').click(); await finished(page);
  assert.equal(requests(control)[0].body.source_lang, 'Chinese'); assert.equal(requests(control)[0].body.document_type, 'general');
  assert.equal(requests(control)[0].body.batch_size, 1);
  control.setup.models.find(model => model.id === '8b').available = false;
  await page.evaluate(() => window.dispatchEvent(new Event('focus'))); await page.waitForTimeout(30);
  await page.locator('#model').selectOption('8b'); assert.equal(await page.locator('#translate').isDisabled(), true);
  assert.match(await page.locator('#model-info').textContent(), /設定・管理からダウンロード/);
  assert.ok(control.requests.every(request => !/prepare|download/.test(request.path)));
  await page.close();
});

test('long prose is sentence-split and hard-bounded without surrounding-source injection', async () => {
  const { page, control } = await fixture();
  const source = 'A condition must hold for every possible integer. '.repeat(90) + '\n\n' + 'AnUnbrokenEnglishWord'.repeat(110);
  await page.locator('#source').fill(source); await page.locator('#translate').click(); await finished(page);
  const sent = allSegments(control);
  assert.ok(sent.length > 7); assert.ok(sent.every(segment => segment.text.length <= 1800));
  assert.ok(sent.filter(segment => segment.text.includes('condition')).every(segment => segment.text.length <= 600));
  assert.ok(requests(control).every(request => request.body.source_context === ''));
  assert.equal(await page.locator('#source').inputValue(), source);
  await page.close();
});

test('a cleared glossary stays empty across setup refresh and reload; edited terms remain authoritative', async () => {
  const { page, control } = await fixture();
  await page.evaluate(() => localStorage.setItem('plamo-glossary', '[]'));
  await page.locator('#source').fill('Find the proper prefixes.'); await page.locator('#translate').click(); await finished(page);
  assert.deepEqual(requests(control).at(-1).body.glossary, []);
  await page.reload(); await page.waitForFunction(() => !document.querySelector('#model').disabled);
  await page.locator('#source').fill('Find the prefix sums.'); await page.locator('#translate').click(); await finished(page);
  assert.deepEqual(requests(control).at(-1).body.glossary, []);
  const edited = [{ source: 'jury', target: '審査員' }];
  await page.evaluate(value => localStorage.setItem('plamo-glossary', JSON.stringify(value)), edited);
  await page.locator('#translate').click(); await finished(page);
  assert.deepEqual(requests(control).at(-1).body.glossary, edited);
  await page.locator('#document-type').selectOption('general');
  await page.evaluate(value => localStorage.setItem('plamo-glossary', JSON.stringify([...value, { source: 'jury', target: '審査員' }])), defaultGlossary.filter(pair => pair.source !== 'jury'));
  await page.locator('#translate').click(); await finished(page);
  assert.deepEqual(requests(control).at(-1).body.glossary, edited);
  await page.close();
});

test('the removed source-context control and legacy preferences never add surrounding source to requests', async () => {
  const { page, control } = await fixture();
  assert.equal(await page.locator('#use-source-context').count(), 0);
  await page.evaluate(() => localStorage.setItem('plamo-text-use-context', 'true'));
  await page.reload(); await page.waitForFunction(() => !document.querySelector('#model').disabled);
  await page.locator('#source').fill('A reference paragraph.\n\nA second paragraph.');
  await page.locator('#translate').click(); await finished(page);
  assert.ok(requests(control).every(request => request.body.source_context === ''));
  assert.match(await page.locator('#source').inputValue(), /reference paragraph/);
  await page.close();
});

test('streamed drafts hide unfinished protection tokens, remain text-only, and give way to verified final results', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#source').fill('The number is $n$.'); await page.locator('#translate').click();
  await until(() => control.jobs.size === 1);
  control.previews.set('text-0', { id: 'text-0', text: '整数 [[PLM_', revision: 1 });
  await page.waitForFunction(() => document.querySelectorAll('.draft').length === 1);
  assert.equal(await page.locator('.draft').textContent(), '整数 …');
  assert.equal(await page.locator('#copy').isDisabled(), true);
  control.previews.set('text-0', { id: 'text-0', text: '<img src=x onerror=alert(1)> [[PLM_0]]', revision: 2 });
  await page.waitForFunction(() => document.querySelector('.draft').textContent.includes('<img'));
  assert.equal(await page.locator('#results img').count(), 0);
  assert.ok((await page.locator('.draft').textContent()).includes('$n$'));
  control.complete('text-0'); await finished(page);
  assert.equal(await page.locator('.draft').count(), 0);
  assert.equal(await page.locator('.translation').textContent(), '訳文: The number is $n$.');
  await page.close();
});

test('streamed drafts cannot borrow a protected formula from another paragraph', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#source').fill('First number $n$.\n\nSecond number $m$.');
  await page.locator('#translate').click(); await until(() => control.jobs.size === 2);
  control.previews.set('text-0', { id: 'text-0', text: '別の段落 [[PLM_1]]', revision: 1 });
  control.previews.set('text-1', { id: 'text-1', text: '正しい段落 [[PLM_1]]', revision: 1 });
  await page.waitForFunction(() => document.querySelector('[data-segment="text-1"]').classList.contains('draft'));
  assert.equal(await page.locator('[data-segment="text-0"]').textContent(), '翻訳待ち…');
  assert.equal(await page.locator('[data-segment="text-1"]').textContent(), '正しい段落 $m$…');
  control.previews.set('text-0', { id: 'text-0', text: '最初の段落 [[PLM_0]]', revision: 2 });
  await page.waitForFunction(() => document.querySelector('[data-segment="text-0"]').classList.contains('draft'));
  assert.equal(await page.locator('[data-segment="text-0"]').textContent(), '最初の段落 $n$…');
  control.finishAll(); await finished(page);
  assert.equal(await page.locator('.draft').count(), 0);
  await page.close();
});

test('stopping a streamed draft restores unfinished source and preserves already verified translations', async () => {
  const { page, control } = await fixture(); control.manual = true;
  await page.locator('#source').fill('A completed paragraph.\n\nAn unfinished paragraph.'); await page.locator('#translate').click();
  await until(() => control.jobs.size === 2);
  control.complete('text-0');
  control.previews.set('text-1', { id: 'text-1', text: 'まだ途中', revision: 1 });
  await page.waitForFunction(() => document.querySelectorAll('.translation').length === 1 && document.querySelectorAll('.draft').length === 1);
  await page.locator('#stop').click();
  assert.equal(await page.locator('.draft').count(), 0);
  assert.equal(await page.locator('.translation').textContent(), '訳文: A completed paragraph.');
  assert.match(await page.locator('#results').textContent(), /An unfinished paragraph/);
  await page.close();
});

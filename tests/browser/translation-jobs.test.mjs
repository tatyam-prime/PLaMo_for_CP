import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const source = await readFile(new URL('../../plamo_cp/assets/translation-jobs.js', import.meta.url), 'utf8');
const start = runInNewContext(source + '\nstartTranslationPipeline;', { setTimeout, clearTimeout, Promise });
const id = index => '00000000-0000-4000-8000-' + String(index).padStart(12, '0');
const segments = count => Array.from({ length: count }, (_, index) => ({ id: String(index), text: 'Paragraph ' + index }));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check) {
  const end = Date.now() + 2000;
  while (!check()) { if (Date.now() > end) throw new Error('Timed out waiting for pipeline state'); await sleep(5); }
}

test('the initial request fills the exact page window and refills each completion before the job ends', async () => {
  const jobs = new Map(), posted = [], results = [], progress = [];
  let sequence = 0;
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') {
      posted.push(body);
      assert.ok(posted.reduce((sum, value) => sum + value.segments.length, 0) - results.length <= 4);
      const job = { id: id(++sequence), segments: body.segments, events: [], state: 'running' };
      jobs.set(job.id, job); return { id: job.id, state: 'queued', cursor: 0 };
    }
    const job = jobs.get(path.split('/')[3].split('?')[0]);
    if (path.endsWith('/cancel')) { job.state = 'cancelled'; return { id: job.id, state: job.state }; }
    const after = Number(new URL(path, 'http://local').searchParams.get('after'));
    return { id: job.id, state: job.state, cursor: job.events.length, events: job.events.filter(event => event.seq > after) };
  };
  function finish(job, paragraph) {
    if (job.events.some(event => event.id === paragraph)) return;
    job.events.push({ seq: job.events.length + 1, type: 'translation', id: paragraph, text: 'Result ' + paragraph, cached: paragraph === '0' });
    if (job.events.length === job.segments.length) job.state = 'completed';
  }
  const pipeline = start({ startIntervalMs: 0, segments: segments(9), batchSize: 4, request, pollInterval: 50,
    contextForChunk: (first, end) => 'Context ' + first + '-' + end, onResult: event => results.push(event), onProgress: value => progress.push(value) });
  await until(() => jobs.size === 1);
  assert.deepEqual(posted.map(body => body.segments.length), [4]);
  const first = [...jobs.values()][0];
  finish(first, '1'); finish(first, '3');
  await until(() => jobs.size === 3);
  assert.equal(first.state, 'running');
  assert.deepEqual(posted.map(body => body.segments.length), [4, 1, 1]);
  const drain = setInterval(() => { for (const job of jobs.values()) for (const paragraph of job.segments) finish(job, paragraph.id); }, 10);
  let result;
  try { result = await pipeline.promise; } finally { clearInterval(drain); }
  assert.equal(result.completed, 9); assert.equal(result.cached, 1); assert.equal(result.cancelled, false);
  assert.equal(new Set(results.map(event => event.id)).size, 9);
  assert.ok(progress.every(value => value.submitted - value.completed <= 4));
  assert.equal(posted[2].source_context, 'Context 5-6');
});

test('Stop before the creation response returns cancels the eventual job without delivering late results', async () => {
  let created, resolveCreation;
  const cancelled = [], results = [];
  const request = (path, body, options) => {
    assert.equal(options?.keepalive, true);
    if (path === '/api/translation-jobs') { created = true; return new Promise(resolve => { resolveCreation = resolve; }); }
    cancelled.push(path); return Promise.resolve({ id: id(1), state: 'cancelled' });
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(1), batchSize: 1, request, onResult: event => results.push(event) });
  await until(() => created);
  pipeline.cancel();
  assert.equal((await pipeline.promise).cancelled, true);
  resolveCreation({ id: id(1), state: 'queued', cursor: 0 });
  await until(() => cancelled.length === 1);
  assert.equal(cancelled[0], '/api/translation-jobs/' + id(1) + '/cancel');
  assert.equal(results.length, 0);
});

test('timed startup admits later paragraphs before any result and Stop clears the remaining ramp', async () => {
  const posted = [], jobs = new Map(), cancelled = [];
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') { const job = { id: id(posted.length + 1), segments: body.segments }; posted.push(body); jobs.set(job.id, job); return { id: job.id }; }
    const job = jobs.get(path.split('/')[3].split('?')[0]);
    if (path.endsWith('/cancel')) { cancelled.push(job.id); return { state: 'cancelled' }; }
    return { id: job.id, state: 'running', cursor: 0, events: [] };
  };
  const pipeline = start({ segments: segments(8), batchSize: 4, startIntervalMs: 50, pollInterval: 50, request });
  await until(() => posted.length === 1);
  assert.equal(posted[0].segments.length, 1);
  await until(() => posted.length === 4);
  assert.deepEqual(posted.map(body => body.segments.length), [1, 1, 1, 1]);
  pipeline.cancel(); await pipeline.promise;
  await sleep(120);
  assert.equal(posted.length, 4); assert.equal(cancelled.length, 4);
});

test('429 backpressure retries the reserved work and delivers cached and per-segment failures exactly once', async () => {
  let attempts = 0;
  const results = [], jobs = new Map();
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') {
      if (++attempts === 1) throw Object.assign(new Error('Busy'), { status: 429 });
      const job = { id: id(attempts), segments: body.segments }; jobs.set(job.id, job); return { id: job.id, state: 'queued', cursor: 0 };
    }
    const job = jobs.get(path.split('/')[3].split('?')[0]);
    return { id: job.id, state: 'completed', cursor: job.segments.length, events: job.segments.map((segment, index) => ({ seq: index + 1, id: segment.id, ...(segment.id === '0' ? { type: 'translation', text: 'Cached result', cached: true } : { type: 'segment_error', message: 'Original retained' }) })) };
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(3), batchSize: 2, request, pollInterval: 50, onResult: event => results.push(event) });
  const result = await pipeline.promise;
  assert.equal(attempts, 3); assert.equal(result.completed, 3); assert.equal(result.cached, 1);
  assert.equal([...jobs.values()][0].segments.length, 2, 'backpressure retries the full reserved initial window');
  assert.deepEqual(results.map(event => event.type), ['translation', 'segment_error', 'segment_error']);
});

test('a failed paragraph frees capacity without stalling later work', async () => {
  const posted = [], jobs = new Map(), results = [];
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') { posted.push(body); const job = { id: id(posted.length), segments: body.segments }; jobs.set(job.id, job); return { id: job.id }; }
    const job = jobs.get(path.split('/')[3].split('?')[0]);
    return { id: job.id, state: 'completed', cursor: job.segments.length, events: job.segments.map((segment, index) => ({ seq: index + 1, id: segment.id, ...(segment.id === '0' ? { type: 'segment_error', message: 'Original retained' } : { type: 'translation', text: 'Result', cached: false }) })) };
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(5), batchSize: 4, request, onResult: event => results.push(event) });
  assert.equal((await pipeline.promise).completed, 5);
  assert.deepEqual(posted.map(body => body.segments.length), [4, 1]);
  assert.equal(results[0].type, 'segment_error');
});

test('a fatal job error preserves delivered output and cancels known and concurrently created jobs', async () => {
  const cancelled = [], results = [];
  let created = 0;
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') return { id: id(++created), state: 'queued', cursor: 0 };
    if (path.endsWith('/cancel')) { cancelled.push(path); return { state: 'cancelled' }; }
    return { id: id(1), state: 'failed', cursor: 1, events: [{ seq: 1, type: 'translation', id: '0', text: 'Completed paragraph', cached: false }], error: { status: 503, message: 'Runtime stopped' } };
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(3), batchSize: 2, request, onResult: event => results.push(event) });
  await assert.rejects(pipeline.promise, /Runtime stopped/);
  await until(() => cancelled.length === 2);
  assert.deepEqual(results.map(event => event.id), ['0']);
});

test('oversized paragraphs are preserved locally and request payloads remain bounded', async () => {
  const posted = [], results = [];
  let sequence = 0;
  const jobs = new Map();
  const request = async (path, body) => {
    if (path === '/api/translation-jobs') {
      assert.ok(JSON.stringify(body).length <= 32000); posted.push(body);
      const job = { id: id(++sequence), segments: body.segments }; jobs.set(job.id, job); return { id: job.id };
    }
    const job = jobs.get(path.split('/')[3].split('?')[0]);
    return { id: job.id, state: 'completed', cursor: job.segments.length, events: job.segments.map((segment, index) => ({ seq: index + 1, type: 'translation', id: segment.id, text: 'Result', cached: false })) };
  };
  const pipeline = start({ startIntervalMs: 0, segments: [{ id: 'huge', text: 'x'.repeat(40000) }, ...segments(4).map(segment => ({ ...segment, text: 'x'.repeat(12000) }))],
    batchSize: 4, request, onResult: event => results.push(event) });
  assert.equal((await pipeline.promise).completed, 5);
  assert.equal(results[0].type, 'segment_error'); assert.equal(results[0].id, 'huge');
  assert.ok(posted.every(body => body.segments.length <= 2));
});

test('a DOM validator failure cancels the job and never delivers later events', async () => {
  const cancelled = [], results = [];
  const request = async path => {
    if (path === '/api/translation-jobs') return { id: id(1) };
    if (path.endsWith('/cancel')) { cancelled.push(path); return { state: 'cancelled' }; }
    return { id: id(1), state: 'completed', cursor: 2, events: [{ seq: 1, type: 'translation', id: '0', text: 'Stale' }, { seq: 2, type: 'translation', id: '1', text: 'Late' }] };
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(2), batchSize: 2, request, onResult: event => { results.push(event.id); throw new Error('DOM changed'); } });
  await assert.rejects(pipeline.promise, /DOM changed/);
  await until(() => cancelled.length === 1);
  assert.deepEqual(results, ['0']);
});

test('integer page windows1..16 work without a powers-of-two restriction and reject invalid limits', async () => {
  for (let count = 1; count <= 16; count++) {
    const jobs = new Map(), posted = [], results = [];
    const request = async (path, body) => {
      if (path === '/api/translation-jobs') {
        posted.push(body); assert.ok(posted.reduce((sum, value) => sum + value.segments.length, 0) - results.length <= count);
        const job = { id: id(posted.length), segments: body.segments }; jobs.set(job.id, job); return { id: job.id };
      }
      const job = jobs.get(path.split('/')[3].split('?')[0]);
      return { id: job.id, state: 'completed', cursor: job.segments.length, events: job.segments.map((segment, index) => ({ seq: index + 1, type: 'translation', id: segment.id, text: 'Result', cached: false })) };
    };
    const pipeline = start({ startIntervalMs: 0, segments: segments(count + 2), batchSize: count, request, onResult: event => results.push(event) });
    assert.equal((await pipeline.promise).completed, count + 2);
    assert.equal(posted[0].segments.length, count);
    assert.ok(posted.every(body => body.batch_size === count));
    assert.ok(posted.slice(1).every(body => body.segments.length <= count));
  }
  for (const invalid of [0, 17, 1.5]) assert.throws(() => start({ startIntervalMs: 0, segments: segments(1), batchSize: invalid, request() {} }), /Invalid translation worklist/);
});

test('preview revisions replace drafts independently of the verified cursor and ignore completed IDs', async () => {
  const drafts = [], results = []; let poll = 0;
  const request = async path => {
    if (path === '/api/translation-jobs') return { id: id(1) };
    if (path.endsWith('/cancel')) return { state: 'cancelled' };
    poll++;
    const preview = (segment, text, revision) => ({ id: segment, text, revision });
    if (poll === 1) return { id: id(1), state: 'running', cursor: 0, events: [], previews: [preview('0', 'First draft', 1)] };
    if (poll === 2) return { id: id(1), state: 'running', cursor: 0, events: [], previews: [preview('0', 'Ignored duplicate revision', 1), preview('1', 'Another draft', 1)] };
    if (poll === 3) return { id: id(1), state: 'running', cursor: 1, events: [{ seq: 1, type: 'translation', id: '0', text: 'Verified first' }], previews: [preview('0', 'Revised prefix', 2)] };
    return { id: id(1), state: 'completed', cursor: 2, events: [{ seq: 2, type: 'translation', id: '1', text: 'Verified second' }], previews: [preview('0', 'Must not reappear', 3)] };
  };
  const pipeline = start({ startIntervalMs: 0, segments: segments(2), batchSize: 2, pollInterval: 50, request, onPreview: draft => drafts.push(draft.text), onResult: event => results.push(event.text) });
  assert.equal((await pipeline.promise).completed, 2);
  assert.deepEqual(drafts, ['First draft', 'Another draft', 'Revised prefix']);
  assert.deepEqual(results, ['Verified first', 'Verified second']);
});

test('unowned or oversized preview snapshots fail closed and cancel their job', async () => {
  for (const preview of [{ id: 'other', text: 'Unknown', revision: 1 }, { id: '0', text: 'x'.repeat(16385), revision: 1 }, { id: '0', text: 'Draft', revision: 1.5 }]) {
    const cancelled = [];
    const request = async path => {
      if (path === '/api/translation-jobs') return { id: id(1) };
      if (path.endsWith('/cancel')) { cancelled.push(path); return { state: 'cancelled' }; }
      return { id: id(1), state: 'running', cursor: 0, events: [], previews: [preview] };
    };
    const pipeline = start({ segments: segments(1), batchSize: 1, request });
    await assert.rejects(pipeline.promise, /途中結果/);
    await until(() => cancelled.length === 1);
  }
});

test('the preview character cap counts Unicode code points like the backend', async () => {
  const text = '😀'.repeat(16384), previews = [];
  const request = async path => path === '/api/translation-jobs' ? { id: id(1) } : {
    id: id(1), state: 'completed', cursor: 1, previews: [{ id: '0', text, revision: 1 }],
    events: [{ seq: 1, type: 'translation', id: '0', text: 'Verified result' }],
  };
  const pipeline = start({ segments: segments(1), batchSize: 1, request, onPreview: preview => previews.push(preview.text) });
  assert.equal((await pipeline.promise).completed, 1);
  assert.deepEqual(previews, [text]);
});

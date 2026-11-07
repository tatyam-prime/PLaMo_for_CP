// Shared by the text workspace and the Tampermonkey bundle.
// The caller owns transport, DOM validation, and presentation.
const TRANSLATION_BATCH_SIZES = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32];

function startTranslationPipeline(options) {
  'use strict';
  const { segments, fields = {}, contextForChunk, request, onResult, onPreview, onProgress } = options;
  const batchSize = Number(options.batchSize ?? 1);
  const validSegment = segment => segment && typeof segment.id === 'string' && segment.id.length > 0 && segment.id.length <= 128 && typeof segment.text === 'string';
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 32 || !Array.isArray(segments) || !segments.length
    || segments.some(segment => !validSegment(segment))
    || new Set(segments.map(segment => segment.id)).size !== segments.length) throw new Error('Invalid translation worklist');
  const worklist = segments.map(segment => ({ ...segment }));
  const knownIds = new Set(worklist.map(segment => segment.id));
  const interval = Math.max(50, Number(options.pollInterval) || 200);
  const startInterval = Math.max(0, Number(options.startIntervalMs ?? 300) || 0);
  const jobs = new Map(), creating = new Set(), timers = new Set(), completedIds = new Set();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let next = 0, outstanding = 0, completed = 0, cached = 0, settled = false, cancelled = false;
  let admissionLimit = startInterval ? 1 : batchSize;
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });

  function later(callback, delay) {
    const timer = setTimeout(() => { timers.delete(timer); if (!settled) callback(); }, delay);
    timers.add(timer);
  }
  function clearTimers() { for (const timer of timers) clearTimeout(timer); timers.clear(); }
  function cancelServer(id) {
    if (!uuid.test(id)) return;
    Promise.resolve().then(() => request(`/api/translation-jobs/${id}/cancel`, {}, { keepalive: true })).catch(() => {});
  }
  function cancelKnown() { for (const id of jobs.keys()) cancelServer(id); }
  function failure(message, status) { const error = new Error(message); error.status = status; return error; }
  function fail(error) {
    if (settled) return;
    settled = true; clearTimers(); cancelKnown(); reject(error);
  }
  function progress() { onProgress?.({ completed, total: worklist.length, outstanding, submitted: next, cached }); }
  function maybeDone() {
    if (!settled && next === worklist.length && !outstanding && !jobs.size && !creating.size) {
      settled = true; clearTimers(); resolve({ completed, total: worklist.length, cached, cancelled: false });
    }
  }
  function accept(job, event) {
    if (!job.ids.has(event.id)) throw failure('翻訳結果の段落IDが一致しません。原文を保持しています。', 502);
    if (completedIds.has(event.id)) return;
    if (event.type !== 'translation' && event.type !== 'segment_error') throw failure('翻訳結果の形式が正しくありません。', 502);
    if (event.type === 'translation' && typeof event.text !== 'string') throw failure('翻訳結果の形式が正しくありません。', 502);
    onResult?.(event);
    completedIds.add(event.id); outstanding--; completed++;
    if (event.type === 'translation' && event.cached === true) cached++;
    progress();
  }
  function missing(job, message) {
    for (const id of job.ids) if (!completedIds.has(id)) accept(job, { type: 'segment_error', id, message });
  }
  async function poll(job) {
    if (settled) return;
    try {
      const result = await request(`/api/translation-jobs/${job.id}?after=${job.cursor}`);
      if (settled) return;
      if (result.id !== job.id || !Array.isArray(result.events) || !Number.isSafeInteger(result.cursor) || result.cursor < job.cursor || result.cursor > 64
        || !['queued', 'running', 'completed', 'cancelled', 'failed'].includes(result.state)) throw failure('翻訳の進捗を受け取れませんでした。', 502);
      if (result.previews !== undefined) {
        if (!Array.isArray(result.previews) || result.previews.length > job.ids.size) throw failure('翻訳の途中結果の形式が正しくありません。', 502);
        const seen = new Set();
        for (const preview of result.previews) {
          if (!job.ids.has(preview.id) || seen.has(preview.id) || typeof preview.text !== 'string' || Array.from(preview.text).length > 16384
            || !Number.isSafeInteger(preview.revision) || preview.revision < 1 || preview.truncated !== undefined && typeof preview.truncated !== 'boolean') throw failure('翻訳の途中結果の形式が正しくありません。', 502);
          seen.add(preview.id);
          if (completedIds.has(preview.id) || preview.revision <= (job.previews.get(preview.id) || 0)) continue;
          job.previews.set(preview.id, preview.revision); onPreview?.(preview);
          if (settled) return;
        }
      }
      let cursor = job.cursor;
      for (const event of result.events) {
        if (settled) return;
        if (!Number.isSafeInteger(event.seq) || event.seq <= cursor || event.seq > result.cursor) throw failure('翻訳の進捗が一致しません。', 502);
        cursor = event.seq;
        accept(job, event);
        if (settled) return;
        // Refill by completed paragraphs while earlier jobs still run.
        fill();
      }
      job.cursor = result.cursor;
      if (result.state === 'failed') {
        if (result.error?.status === 422) missing(job, result.error.message || '翻訳できなかった段落は原文を保持しています。');
        else throw failure(result.error?.message || '翻訳処理を完了できませんでした。', result.error?.status || 503);
      } else if (result.state === 'cancelled') throw failure(result.error?.message || '翻訳がキャンセルされました。', 499);
      else if (result.state === 'completed') missing(job, '翻訳結果を受け取れなかった段落は原文を保持しています。');
      if (['completed', 'failed'].includes(result.state)) jobs.delete(job.id);
      else later(() => poll(job), interval);
      fill(); maybeDone();
    } catch (error) { if (!settled) fail(error); }
  }
  async function create(entry) {
    if (settled) { creating.delete(entry); return; }
    try {
      // Keep the response so a Stop before202 can still cancel its eventual ID.
      const result = await request('/api/translation-jobs', entry.payload, { keepalive: true });
      creating.delete(entry);
      if (!uuid.test(result.id || '')) throw failure('翻訳ジョブを開始できませんでした。', 502);
      if (settled) { cancelServer(result.id); return; }
      if (jobs.has(result.id)) throw failure('翻訳ジョブIDが重複しています。', 502);
      const job = { id: result.id, cursor: 0, ids: new Set(entry.segments.map(segment => segment.id)), previews: new Map() };
      jobs.set(job.id, job); poll(job);
    } catch (error) {
      if (settled) { creating.delete(entry); return; }
      if (error.status === 429) {
        entry.retries++;
        later(() => create(entry), Math.min(2000, interval * 2 ** Math.min(entry.retries - 1, 4)));
      } else if (error.status === 422) {
        creating.delete(entry);
        try {
          missing({ ids: new Set(entry.segments.map(segment => segment.id)) }, error.message || '翻訳できなかった段落は原文を保持しています。');
          fill(); maybeDone();
        } catch (callbackError) { fail(callbackError); }
      } else { creating.delete(entry); fail(error); }
    }
  }
  function fill() {
    if (settled) return;
    while (next < worklist.length) {
      let count = Math.min(admissionLimit - outstanding, batchSize, worklist.length - next);
      if (count <= 0) break;
      const start = next;
      const makePayload = size => ({ ...fields,
        source_context: contextForChunk ? contextForChunk(start, start + size) : fields.source_context || '',
        batch_size: batchSize, segments: worklist.slice(start, start + size) });
      let payload = makePayload(count);
      while (count > 1 && JSON.stringify(payload).length > 32000) payload = makePayload(--count);
      next += count; outstanding += count;
      if (JSON.stringify(payload).length > 32000) {
        missing({ ids: new Set(payload.segments.map(segment => segment.id)) }, '段落が長すぎるため原文を保持しています。');
        continue;
      }
      const entry = { start, end: next, segments: payload.segments, payload, retries: 0 };
      creating.add(entry); create(entry);
    }
    maybeDone();
  }
  function cancel() {
    if (settled) return;
    cancelled = true; settled = true; clearTimers(); cancelKnown();
    // Pending creation callbacks retain their ID and cancel it on arrival.
    resolve({ completed, total: worklist.length, cached, cancelled });
  }
  function enqueue(additional, { priority = false } = {}) {
    if (settled) throw new Error('Cannot enqueue translation work after the pipeline has ended');
    if (contextForChunk) throw new Error('Dynamic translation work does not support contextForChunk');
    if (!Array.isArray(additional) || !additional.length || additional.some(segment => !validSegment(segment))
      || new Set(additional.map(segment => segment.id)).size !== additional.length
      || additional.some(segment => knownIds.has(segment.id))) throw new Error('Invalid or duplicate translation work IDs');
    const copied = additional.map(segment => ({ ...segment }));
    for (const segment of copied) knownIds.add(segment.id);
    if (priority) worklist.splice(next, 0, ...copied);
    else worklist.push(...copied);
    // onResult runs before accept() releases its slot. Defer this refill so
    // callback additions cannot recursively admit work against stale counts.
    Promise.resolve().then(() => {
      if (!settled) { try { progress(); fill(); } catch (error) { fail(error); } }
    });
  }
  function ramp() {
    if (settled || admissionLimit >= batchSize) return;
    try {
      admissionLimit++; fill();
      if (!settled && admissionLimit < batchSize) later(ramp, startInterval);
    } catch (error) { fail(error); }
  }
  Promise.resolve().then(() => { if (!settled) { try { progress(); fill(); if (!settled && admissionLimit < batchSize) later(ramp, startInterval); } catch (error) { fail(error); } } });
  return { promise, cancel, enqueue };
}

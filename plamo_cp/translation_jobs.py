"""Bounded in-memory translation jobs sharing one model worker across clients."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .engine import TranslationWork, preview_prefix

MAX_PREVIEW_CHARACTERS = 16384
PREVIEW_INTERVAL = 0.05
DEFAULT_MAX_ACTIVE = 32
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
log = logging.getLogger(__name__)


class JobQueueFull(RuntimeError):
    pass


@dataclass
class TranslationJob:
    id: str
    payload: Any
    queued: deque
    total: int
    strict: bool
    lease: bool
    last_poll: float
    state: str = "queued"
    events: list[dict] = field(default_factory=list)
    previews: dict[str, dict] = field(default_factory=dict)
    preview_times: dict[str, float] = field(default_factory=dict)
    preview_revision: int = 0
    completed: int = 0
    cached: int = 0
    active: int = 0
    error: dict | None = None
    finished_at: float | None = None
    retain: bool = True
    delivered: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


class TranslationScheduler:
    def __init__(
        self,
        engine,
        cache,
        executor: ThreadPoolExecutor,
        key,
        extra,
        verify,
        *,
        clock=time.monotonic,
        max_jobs=32,
        max_segments=512,
        lease_seconds=15,
        result_ttl=60,
        max_active=DEFAULT_MAX_ACTIVE,
    ):
        self.engine, self.cache, self.executor = engine, cache, executor
        self.key, self.extra, self.verify = key, extra, verify
        self.clock, self.max_jobs, self.max_segments = clock, max_jobs, max_segments
        self.lease_seconds, self.result_ttl, self.max_active = lease_seconds, result_ttl, max_active
        self._lock = threading.RLock()
        self._jobs: dict[str, TranslationJob] = {}
        self._ready: deque[str] = deque()
        self._inflight: dict[tuple[str, int], TranslationWork] = {}
        self._pending = 0
        self._running = False
        self._closed = threading.Event()
        self._reaper = None
        self._cohort_model = None
        self._cohort_completed = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running or self._pending > 0

    def submit(self, payload, *, strict=False, lease=True) -> TranslationJob:
        with self._lock:
            self._reap_locked()
            if self._closed.is_set():
                raise RuntimeError("翻訳アプリを終了しています。")
            # Results normally live for 60 seconds. Under pressure, discard the
            # oldest delivered, fully drained jobs before rejecting fresh work.
            # A fast cached job must survive until its client polls the result.
            while len(self._jobs) >= self.max_jobs:
                disposable = next(
                    (
                        job
                        for job in self._jobs.values()
                        if job.state in TERMINAL_STATES and not job.active and job.delivered
                    ),
                    None,
                )
                if disposable is None:
                    raise JobQueueFull("翻訳の待ち件数が多いため、少し待ってからお試しください。")
                self._jobs.pop(disposable.id)
            if self._pending + len(payload.segments) > self.max_segments:
                raise JobQueueFull("翻訳の待ち段落数が多いため、少し待ってからお試しください。")
            identifier = str(uuid.uuid4())
            queued = deque(
                (index, segment, self.key(payload, segment))
                for index, segment in enumerate(payload.segments)
            )
            job = TranslationJob(
                identifier, payload, queued, len(payload.segments), strict, lease, self.clock()
            )
            self._jobs[identifier] = job
            self._ready.append(identifier)
            self._pending += job.total
            if self._reaper is None:
                self._reaper = threading.Thread(
                    target=self._lease_loop, name="plamo-job-leases", daemon=True
                )
                self._reaper.start()
            if not self._running:
                self._running = True
                self.executor.submit(self._drain)
            return job

    def poll(self, identifier: str, after: int = 0) -> dict:
        with self._lock:
            self._reap_locked()
            job = self._jobs.get(identifier)
            if job is None:
                raise KeyError(identifier)
            if not isinstance(after, int) or after < 0 or after > len(job.events):
                raise ValueError("翻訳結果の取得位置が正しくありません。")
            if job.state not in TERMINAL_STATES:
                job.last_poll = self.clock()
            value = {
                "id": job.id,
                "state": job.state,
                "events": [dict(event) for event in job.events[after:]],
                "cursor": len(job.events),
                "completed": job.completed,
                "total": job.total,
                "cached": job.cached,
                "previews": [dict(value) for value in job.previews.values()],
            }
            if job.error:
                value["error"] = dict(job.error)
            if job.state in TERMINAL_STATES:
                job.delivered = True
            return value

    def cancel(self, identifier: str) -> dict:
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None:
                raise KeyError(identifier)
            self._cancel_locked(job)
            # An explicit Stop relinquishes unread output; lease expiry does
            # not, so disconnected clients can still retrieve their result.
            job.delivered = True
            return {"id": identifier, "state": job.state}

    def release(self, identifier: str) -> None:
        """Legacy HTTP requests need no retained polling result after they end."""
        with self._lock:
            job = self._jobs.get(identifier)
            if job is not None:
                job.retain = False
            self._reap_locked()

    def reap(self) -> None:
        with self._lock:
            self._reap_locked()

    def _reap_locked(self) -> None:
        now = self.clock()
        for job in list(self._jobs.values()):
            if (
                job.state not in TERMINAL_STATES
                and job.lease
                and now - job.last_poll >= self.lease_seconds
            ):
                self._cancel_locked(job, "ブラウザーとの接続が途切れたため翻訳を停止しました。")
            if (
                job.state in TERMINAL_STATES
                and not job.active
                and (not job.retain or now - job.finished_at >= self.result_ttl)
            ):
                self._jobs.pop(job.id, None)

    def _cancel_locked(self, job, message="翻訳を停止しました。") -> None:
        if job.state in TERMINAL_STATES:
            return
        job.cancelled.set()
        job.previews.clear()
        job.preview_times.clear()
        self._pending -= len(job.queued)
        job.queued.clear()
        self._ready = deque(identifier for identifier in self._ready if identifier != job.id)
        job.state = "cancelled"
        job.error = {"status": 499, "message": message}
        job.finished_at = self.clock()
        job.done.set()

    def _fail_locked(self, job, error, status=503) -> None:
        if job.state in TERMINAL_STATES:
            return
        job.cancelled.set()
        job.previews.clear()
        job.preview_times.clear()
        self._pending -= len(job.queued)
        job.queued.clear()
        self._ready = deque(identifier for identifier in self._ready if identifier != job.id)
        job.state = "failed"
        job.error = {"status": status, "message": str(error)}
        job.finished_at = self.clock()
        job.done.set()

    def _lease_loop(self):
        while not self._closed.wait(1):
            self.reap()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            for job in self._jobs.values():
                self._cancel_locked(job)

    def _next_model_locked(self, avoid=None):
        candidates = [
            self._jobs[identifier]
            for identifier in self._ready
            if identifier in self._jobs
            and self._jobs[identifier].queued
            and self._jobs[identifier].state not in TERMINAL_STATES
        ]
        return next(
            (job.payload.model for job in candidates if job.payload.model != avoid),
            candidates[0].payload.model if candidates else None,
        )

    def _take(self, model, count):
        result = []
        while len(result) < count:
            with self._lock:
                self._reap_locked()
                if self._closed.is_set():
                    return result
                if self._cohort_completed >= 64 and self._next_model_locked(avoid=model) not in {
                    None,
                    model,
                }:
                    return result
                chosen = None
                for _ in range(len(self._ready)):
                    identifier = self._ready.popleft()
                    job = self._jobs.get(identifier)
                    if job is None or job.state in TERMINAL_STATES or not job.queued:
                        continue
                    if job.payload.model != model:
                        self._ready.append(identifier)
                        continue
                    chosen = job
                    index, segment, key = job.queued.popleft()
                    if job.queued:
                        self._ready.append(identifier)
                    job.active += 1
                    job.state = "running"
                    parameters = {
                        "source_lang": job.payload.source_lang,
                        "target_lang": job.payload.target_lang,
                        **self.extra(job.payload),
                    }
                    work = TranslationWork(
                        (identifier, index), segment.text, parameters, job.cancelled
                    )
                    work.cache_key, work.segment_id = key, segment.id
                    self._inflight[work.key] = work
                    if job.lease and getattr(self.engine, "supported_translation_previews", False):
                        work.progress = lambda text, key=work.key: self._preview(key, text)
                    break
                if chosen is None:
                    return result
            translated = self.cache.get(work.cache_key)
            if translated is not None:
                try:
                    self.verify(work.text, translated)
                except ValueError:
                    translated = None
            if translated is None:
                result.append(work)
            else:
                self._complete(work, translated, cached=True)
        return result

    def _preview(self, key, text):
        if not isinstance(text, str):
            return
        text = preview_prefix(text)
        if not text.strip():
            return
        with self._lock:
            work = self._inflight.get(key)
            if work is None or work.cancelled.is_set():
                return
            job = self._jobs.get(key[0])
            if job is None or job.state in TERMINAL_STATES:
                return
            now = self.clock()
            previous_time = job.preview_times.get(work.segment_id)
            if previous_time is not None and now - previous_time < PREVIEW_INTERVAL:
                return
            clipped = text[:MAX_PREVIEW_CHARACTERS]
            truncated = len(text) > MAX_PREVIEW_CHARACTERS
            previous = job.previews.get(work.segment_id)
            if (
                previous is not None
                and previous["text"] == clipped
                and bool(previous.get("truncated")) == truncated
            ):
                return
            job.preview_revision += 1
            value = {"id": work.segment_id, "text": clipped, "revision": job.preview_revision}
            if truncated:
                value["truncated"] = True
            job.previews[work.segment_id] = value
            job.preview_times[work.segment_id] = now

    def _complete(self, work, result, *, cached=False):
        with self._lock:
            if self._inflight.pop(work.key, None) is None:
                return
            self._pending -= 1
            self._cohort_completed += 1
            job = self._jobs[work.key[0]]
            job.active -= 1
            job.previews.pop(work.segment_id, None)
            job.preview_times.pop(work.segment_id, None)
            if job.state in TERMINAL_STATES:
                return
            if isinstance(result, InterruptedError):
                self._cancel_locked(job, str(result))
                return
            try:
                if isinstance(result, ValueError):
                    raise result
                self.verify(work.text, result)
            except ValueError as error:
                if job.strict:
                    self._fail_locked(job, error, 422)
                    return
                event = {"type": "segment_error", "id": work.segment_id, "message": str(error)}
            else:
                if not cached:
                    self.cache.put(work.cache_key, result)
                event = {
                    "type": "translation",
                    "id": work.segment_id,
                    "text": result,
                    "cached": cached,
                }
                job.cached += int(cached)
            job.completed += 1
            job.events.append({"seq": len(job.events) + 1, **event})
            if job.completed == job.total:
                job.previews.clear()
                job.preview_times.clear()
                job.state = "completed"
                job.finished_at = self.clock()
                job.done.set()

    def _fallback(self, model):
        # Windows has one CPU slot. Small test/legacy engines without an MLX
        # completion iterator retain their declared batch capability.
        capacity = max(getattr(self.engine, "supported_batch_sizes", (1,)))
        while works := self._take(model, capacity):
            strict = any(self._jobs[work.key[0]].strict for work in works)
            if len(works) == 1 or strict or not hasattr(self.engine, "translate_batch"):
                for work in works:
                    try:
                        if work.cancelled.is_set():
                            raise InterruptedError("翻訳を停止しました。")
                        result = self.engine.translate(
                            work.text, model=model, cancelled=work.cancelled, **work.parameters
                        )
                    except (ValueError, InterruptedError) as error:
                        result = error
                    self._complete(work, result)
            else:
                options = works[0].parameters
                # Independent request options must never be replaced with
                # another client's context by a legacy batch-only engine.
                if any(work.parameters != options for work in works):
                    for work in works:
                        try:
                            result = self.engine.translate(
                                work.text, model=model, cancelled=work.cancelled, **work.parameters
                            )
                        except (ValueError, InterruptedError) as error:
                            result = error
                        self._complete(work, result)
                    continue
                outcomes = self.engine.translate_batch(
                    [work.text for work in works], model=model, cancelled=self._closed, **options
                )
                if len(outcomes) != len(works):
                    raise RuntimeError("翻訳エンジンの段落数が一致しません。")
                for work, result in zip(works, outcomes, strict=True):
                    self._complete(work, result)

    def _drain(self):
        avoid = None
        while True:
            with self._lock:
                model = self._next_model_locked(avoid=avoid)
                if model is None or self._closed.is_set():
                    self._running = False
                    return
                self._cohort_model, self._cohort_completed = model, 0
            try:
                if hasattr(self.engine, "iter_work"):
                    stream = self.engine.iter_work(
                        model,
                        lambda count, selected=model: self._take(selected, count),
                        lambda: self.max_active,
                        cancelled=self._closed,
                        prefill_batch_size=1,
                        prefill_step_size=256,
                    )
                    try:
                        for work, result in stream:
                            self._complete(work, result)
                    finally:
                        stream.close()
                else:
                    self._fallback(model)
            except Exception as error:
                log.exception("Translation queue failed")
                status = (
                    499
                    if isinstance(error, InterruptedError)
                    else 422
                    if isinstance(error, ValueError)
                    else 503
                )
                with self._lock:
                    for job in self._jobs.values():
                        if job.payload.model == model and job.state not in TERMINAL_STATES:
                            self._fail_locked(job, error, status)
                    for key, work in list(self._inflight.items()):
                        if self._jobs[key[0]].payload.model == model:
                            self._inflight.pop(key)
                            self._jobs[key[0]].active -= 1
                            self._pending -= 1
            avoid = model if self._cohort_completed >= 64 else None

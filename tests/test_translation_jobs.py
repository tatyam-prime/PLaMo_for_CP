"""Job delivery and lifecycle are exercised without MLX, downloads, or GPUs."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import TranslationRequest, create_app
from plamo_cp.cache import TranslationCache
from plamo_cp.translation_jobs import JobQueueFull, TranslationScheduler

HEADERS = {"X-Plamo-Client": "userscript"}
BASE = "/api/translation-jobs"
TERMINAL = {"completed", "cancelled", "failed"}


class ControlledEngine:
    supported_batch_sizes = (1, 2, 4, 8)

    def __init__(self):
        self.status = {"state": "idle", "loaded_model": None}
        self.calls = []
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.outputs = {}

    def translate(self, text, *, model, cancelled=None, **options):
        self.calls.append((text, threading.get_ident(), options))
        if text.startswith("Wait"):
            self.blocked.set()
            deadline = time.monotonic() + 5
            while not self.release.wait(0.005):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                if time.monotonic() >= deadline:
                    raise RuntimeError("The test worker was not released")
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("翻訳を停止しました。")
        return self.outputs.get(text, "日本語: " + text)


def payload(*texts, **options):
    return {
        "model": "0.8b",
        "segments": [{"id": f"s{index}", "text": text} for index, text in enumerate(texts)],
        **options,
    }


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("The expected worker transition did not happen")


def terminal(client, identifier):
    def check():
        response = client.get(f"{BASE}/{identifier}", headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        return result if result["state"] in TERMINAL else None

    return eventually(check)


@pytest.fixture
def api(tmp_path):
    engine = ControlledEngine()
    app = create_app(engine, tmp_path / "cache.sqlite3", tmp_path / "settings.json")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            yield client, engine
        finally:
            engine.release.set()


def test_completed_paragraph_is_delivered_and_cached_before_the_job_finishes(api):
    client, engine = api
    response = client.post(
        BASE, json=payload("Find the subtotal.", "Wait for the next paragraph."), headers=HEADERS
    )
    assert response.status_code == 202
    job = response.json()
    assert str(uuid.UUID(job["id"])) == job["id"]
    assert job["state"] == "queued" and job["cursor"] == 0 and job["total"] == 2
    assert engine.blocked.wait(2)
    state = client.get(f"{BASE}/{job['id']}", headers=HEADERS).json()
    assert state["state"] == "running" and state["completed"] == 1 and state["total"] == 2
    assert state["events"] == [
        {
            "seq": 1,
            "type": "translation",
            "id": "s0",
            "text": "日本語: Find the subtotal.",
            "cached": False,
        }
    ]
    assert client.get("/api/cache").json()["entries"] == 1
    delta = client.get(f"{BASE}/{job['id']}?after=1", headers=HEADERS).json()
    assert delta["events"] == [] and delta["cursor"] == 1
    assert client.post(f"{BASE}/{job['id']}/cancel", json={}, headers=HEADERS).status_code == 200
    cached = client.post(BASE, json=payload("Find the subtotal."), headers=HEADERS).json()
    result = terminal(client, cached["id"])
    assert result["state"] == "completed" and result["cached"] == 1
    assert result["events"][0]["cached"] is True
    assert len([call for call in engine.calls if call[0] == "Find the subtotal."]) == 1


def test_cancelling_one_job_does_not_cancel_a_queued_job_with_reused_segment_ids(api):
    client, engine = api
    first = client.post(BASE, json=payload("Wait for cancellation."), headers=HEADERS).json()
    assert engine.blocked.wait(2)
    second = client.post(BASE, json=payload("Find another answer."), headers=HEADERS).json()
    cancelled = client.post(f"{BASE}/{first['id']}/cancel", json={}, headers=HEADERS)
    assert cancelled.json()["state"] == "cancelled"
    result = terminal(client, second["id"])
    assert result["state"] == "completed"
    assert result["events"][0]["id"] == "s0"
    assert result["events"][0]["text"] == "日本語: Find another answer."
    assert terminal(client, first["id"])["events"] == []


def test_legacy_translation_and_polling_jobs_use_the_same_worker_and_keep_options(api):
    client, engine = api
    first = client.post(
        BASE,
        json=payload("Wait for the other client.", source_context="First client's context."),
        headers=HEADERS,
    ).json()
    assert engine.blocked.wait(2)
    with ThreadPoolExecutor(max_workers=1) as callers:
        future = callers.submit(
            client.post,
            "/api/translate",
            json=payload("Translate independently.", source_context="Second client's context."),
            headers=HEADERS,
        )
        eventually(lambda: client.app.state.translation_jobs._pending == 2)
        client.post(f"{BASE}/{first['id']}/cancel", json={}, headers=HEADERS)
        result = future.result(timeout=3)
    assert result.status_code == 200, result.text
    assert result.json()["translations"] == [
        {"id": "s0", "text": "日本語: Translate independently."}
    ]
    assert len({call[1] for call in engine.calls}) == 1
    assert [call[2]["source_context"] for call in engine.calls] == [
        "First client's context.",
        "Second client's context.",
    ]


def test_segment_error_keeps_later_completions_and_never_caches_invalid_output(api):
    client, engine = api
    engine.outputs["Find [[PLM_0]]."] = "保護記号が欠けた訳文"
    job = client.post(
        BASE, json=payload("Find [[PLM_0]].", "Print the valid result."), headers=HEADERS
    ).json()
    state = terminal(client, job["id"])
    assert state["state"] == "completed" and state["completed"] == 2
    assert [event["type"] for event in state["events"]] == ["segment_error", "translation"]
    assert state["events"][0]["id"] == "s0" and state["events"][1]["id"] == "s1"
    assert client.get("/api/cache").json()["entries"] == 1


def test_polling_and_legacy_clients_merge_into_one_model_iterator_without_context_crosstalk(
    tmp_path,
):
    class JoiningEngine(ControlledEngine):
        def __init__(self):
            super().__init__()
            self.collect = threading.Event()
            self.started = threading.Event()
            self.runs = []
            self.groups = []

        def iter_work(self, model, take, capacity, *, cancelled, **_options):
            self.runs.append((model, threading.get_ident()))
            self.started.set()
            if not self.collect.wait(3):
                raise RuntimeError("The test clients did not join")
            while works := take(capacity()):
                self.groups.append(works)
                # Completion order intentionally differs from request order.
                for work in reversed(works):
                    yield work, "日本語: " + work.text

    engine = JoiningEngine()
    app = create_app(engine, tmp_path / "joined.sqlite3", tmp_path / "joined-settings.json")
    with (
        TestClient(app, base_url="http://127.0.0.1") as client,
        ThreadPoolExecutor(max_workers=1) as callers,
    ):
        try:
            first = client.post(
                BASE,
                json=payload("The first client's paragraph.", source_context="A river bank."),
                headers=HEADERS,
            ).json()
            assert engine.started.wait(2)
            second = callers.submit(
                client.post,
                "/api/translate",
                json=payload(
                    "The other client's paragraph.", source_context="A financial bank.", glossary=[]
                ),
                headers=HEADERS,
            )
            eventually(lambda: app.state.translation_jobs._pending == 2)
            engine.collect.set()
            result = second.result(timeout=3)
            assert result.status_code == 200
            assert (
                result.json()["translations"][0]["text"] == "日本語: The other client's paragraph."
            )
            assert (
                terminal(client, first["id"])["events"][0]["text"]
                == "日本語: The first client's paragraph."
            )
            assert len(engine.runs) == 1 and len(engine.groups[0]) == 2
            assert len({work.key[0] for work in engine.groups[0]}) == 2
            assert "glossary" not in engine.groups[0][0].parameters
            assert engine.groups[0][1].parameters["glossary"] == []
            assert [work.parameters["source_context"] for work in engine.groups[0]] == [
                "A river bank.",
                "A financial bank.",
            ]
            assert engine.calls == [], "both endpoints must use the shared completion iterator"
        finally:
            engine.collect.set()


def test_job_endpoints_require_the_userscript_header_and_validate_id_and_cursor(api):
    client, engine = api
    assert client.post(BASE, json=payload("Find the answer.")).status_code == 403
    assert (
        client.post(
            BASE, json=payload("Find the answer."), headers={"X-Plamo-Client": "setup"}
        ).status_code
        == 403
    )
    job = client.post(BASE, json=payload("Find the answer."), headers=HEADERS).json()
    terminal(client, job["id"])
    for headers in [{}, {"X-Plamo-Client": "setup"}]:
        assert client.get(f"{BASE}/{job['id']}", headers=headers).status_code == 403
        assert client.post(f"{BASE}/{job['id']}/cancel", headers=headers).status_code == 403
    unknown = str(uuid.uuid4())
    assert client.get(f"{BASE}/{unknown}", headers=HEADERS).status_code == 404
    assert client.post(f"{BASE}/{unknown}/cancel", headers=HEADERS).status_code == 404
    assert client.get(f"{BASE}/not-a-uuid", headers=HEADERS).status_code == 422
    for after in [-1, 2, 65]:
        assert client.get(f"{BASE}/{job['id']}?after={after}", headers=HEADERS).status_code == 422
    malformed = payload("One paragraph.", "Another paragraph.")
    malformed["segments"][1]["id"] = "s0"
    before = len(engine.calls)
    assert client.post(BASE, json=malformed, headers=HEADERS).status_code == 422
    assert len(engine.calls) == before


@pytest.fixture
def scheduler_factory(tmp_path):
    resources = []

    def create(**options):
        engine = ControlledEngine()
        cache = TranslationCache(tmp_path / f"cache-{len(resources)}.sqlite3")
        executor = ThreadPoolExecutor(max_workers=1)
        scheduler = TranslationScheduler(
            engine,
            cache,
            executor,
            key=lambda request, segment: segment.text,
            extra=lambda request: {},
            verify=lambda source, result: None,
            **options,
        )
        resources.append((scheduler, executor, cache, engine))
        return scheduler, engine

    yield create
    for scheduler, executor, cache, engine in resources:
        scheduler.close()
        engine.release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        cache.close()


def test_poll_renews_active_lease_expiry_cancels_work_and_terminal_results_expire(
    scheduler_factory,
):
    now = [100.0]
    scheduler, engine = scheduler_factory(clock=lambda: now[0], lease_seconds=5, result_ttl=10)
    request = TranslationRequest.model_validate(payload("Wait for lease expiry."))
    job = scheduler.submit(request)
    assert engine.blocked.wait(2)
    now[0] = 104
    assert scheduler.poll(job.id)["state"] == "running"
    now[0] = 108
    scheduler.reap()
    assert job.state == "running"
    now[0] = 110
    scheduler.reap()
    assert job.cancelled.is_set() and job.state == "cancelled" and job.done.is_set()
    eventually(lambda: not scheduler.busy)
    now[0] = 121
    scheduler.reap()
    with pytest.raises(KeyError):
        scheduler.poll(job.id)


def test_job_and_segment_bounds_release_capacity_after_cancellation(scheduler_factory):
    scheduler, engine = scheduler_factory(max_jobs=1, max_segments=2)
    request = TranslationRequest.model_validate(payload("Wait for a slot."))
    job = scheduler.submit(request, lease=False)
    assert engine.blocked.wait(2)
    with pytest.raises(JobQueueFull):
        scheduler.submit(request)
    scheduler.cancel(job.id)
    eventually(lambda: not scheduler.busy)
    replacement = scheduler.submit(TranslationRequest.model_validate(payload("A fresh slot.")))
    assert replacement.done.wait(2) and replacement.state == "completed"


@pytest.mark.parametrize("cached", [False, True])
def test_job_pressure_preserves_unread_completed_results_until_terminal_poll(
    scheduler_factory, cached
):
    scheduler, engine = scheduler_factory(max_jobs=1)
    if cached:
        scheduler.cache.put("The first paragraph.", "保存済みの訳文。")
    first = scheduler.submit(TranslationRequest.model_validate(payload("The first paragraph.")))
    assert first.done.wait(2)
    eventually(lambda: not scheduler.busy)
    assert first.state == "completed" and first.delivered is False
    assert len(engine.calls) == (0 if cached else 1)
    replacement = TranslationRequest.model_validate(payload("The next paragraph."))
    with pytest.raises(JobQueueFull):
        scheduler.submit(replacement)
    result = scheduler.poll(first.id)
    assert result["events"][0]["id"] == "s0"
    assert result["events"][0]["cached"] is cached
    assert first.delivered is True
    second = scheduler.submit(replacement)
    assert second.done.wait(2)
    with pytest.raises(KeyError):
        scheduler.poll(first.id)


def test_partial_poll_does_not_acknowledge_future_unread_terminal_results(scheduler_factory):
    scheduler, engine = scheduler_factory(max_jobs=1)
    job = scheduler.submit(
        TranslationRequest.model_validate(
            payload("First paragraph.", "Wait for the final paragraph.")
        )
    )
    assert engine.blocked.wait(2)
    partial = scheduler.poll(job.id)
    assert partial["state"] == "running" and partial["cursor"] == 1
    assert job.delivered is False
    engine.release.set()
    assert job.done.wait(2)
    eventually(lambda: not scheduler.busy)
    with pytest.raises(JobQueueFull):
        scheduler.submit(TranslationRequest.model_validate(payload("Another page.")))
    final = scheduler.poll(job.id, after=partial["cursor"])
    assert final["state"] == "completed"
    assert [event["id"] for event in final["events"]] == ["s1"]
    assert job.delivered is True


def test_segment_bound_releases_capacity_after_cancellation(scheduler_factory):
    scheduler, engine = scheduler_factory(max_jobs=3, max_segments=1)
    request = TranslationRequest.model_validate(payload("Wait for a slot."))
    job = scheduler.submit(request, lease=False)
    assert engine.blocked.wait(2)
    with pytest.raises(JobQueueFull):
        scheduler.submit(TranslationRequest.model_validate(payload("Too many segments.")))
    scheduler.cancel(job.id)
    eventually(lambda: not scheduler.busy)
    replacement = scheduler.submit(
        TranslationRequest.model_validate(payload("Capacity is available."))
    )
    assert replacement.done.wait(2) and replacement.state == "completed"


def test_close_cancels_active_and_queued_jobs_and_rejects_new_work(scheduler_factory):
    scheduler, engine = scheduler_factory()
    first = scheduler.submit(TranslationRequest.model_validate(payload("Wait during shutdown.")))
    assert engine.blocked.wait(2)
    second = scheduler.submit(TranslationRequest.model_validate(payload("Queued during shutdown.")))
    scheduler.close()
    assert first.done.wait(2) and second.done.wait(2)
    eventually(lambda: not scheduler.busy)
    assert first.state == second.state == "cancelled"
    with pytest.raises(RuntimeError, match="終了"):
        scheduler.submit(TranslationRequest.model_validate(payload("After shutdown.")))


def test_latest_previews_are_bounded_revisable_uncached_and_separate_from_event_cursor(api):
    client, engine = api
    engine.supported_translation_previews = True
    scheduler = client.app.state.translation_jobs
    now = [100.0]
    scheduler.clock = lambda: now[0]
    source = "Wait for [[PLM_0]]."
    identifier = client.post(BASE, json=payload(source), headers=HEADERS).json()["id"]
    assert engine.blocked.wait(2)
    work = next(iter(scheduler._inflight.values()))
    work.progress("未検証の途中訳")
    first = client.get(f"{BASE}/{identifier}", headers=HEADERS).json()
    assert first["previews"] == [{"id": "s0", "text": "未検証の途中訳", "revision": 1}]
    assert first["events"] == [] and first["cursor"] == 0
    assert client.get("/api/cache").json()["entries"] == 0
    first["previews"][0]["text"] = "outside mutation"
    work.progress("This update is too early")
    assert scheduler.poll(identifier)["previews"][0]["text"] == "未検証の途中訳"
    now[0] += 0.1
    work.progress("前の接頭辞も変更できる<|plamo:tag|>hidden")
    second = scheduler.poll(identifier)
    assert second["previews"] == [{"id": "s0", "text": "前の接頭辞も変更できる", "revision": 2}]
    now[0] += 0.1
    work.progress("訳" * 20000)
    bounded = scheduler.poll(identifier)["previews"][0]
    assert len(bounded["text"]) == 16384 and bounded["truncated"] is True
    revision = bounded["revision"]
    now[0] += 0.1
    work.progress("訳" * 25000)
    assert scheduler.poll(identifier)["previews"][0]["revision"] == revision
    engine.release.set()
    result = terminal(client, identifier)
    assert result["previews"] == [] and result["cursor"] == 1
    assert result["events"][0]["text"] == "日本語: " + source
    assert client.get("/api/cache").json()["entries"] == 1
    work.progress("Late preview must not revive")
    assert scheduler.poll(identifier)["previews"] == []
    cached = client.post(BASE, json=payload(source), headers=HEADERS).json()["id"]
    result = terminal(client, cached)
    assert result["cached"] == 1 and result["previews"] == []


@pytest.mark.parametrize("action", ["cancel", "lease", "close"])
def test_cancel_lease_and_shutdown_clear_previews_and_ignore_late_callbacks(api, action):
    client, engine = api
    engine.supported_translation_previews = True
    scheduler = client.app.state.translation_jobs
    now = [100.0]
    scheduler.clock = lambda: now[0]
    identifier = client.post(BASE, json=payload("Wait for a draft."), headers=HEADERS).json()["id"]
    assert engine.blocked.wait(2)
    work = next(iter(scheduler._inflight.values()))
    work.progress("未検証")
    assert scheduler.poll(identifier)["previews"]
    if action == "cancel":
        scheduler.cancel(identifier)
    elif action == "lease":
        now[0] += 16
        scheduler.reap()
    else:
        scheduler.close()
    work.progress("Late preview")
    result = scheduler.poll(identifier)
    assert result["state"] == "cancelled" and result["previews"] == [] and result["events"] == []
    assert client.get("/api/cache").json()["entries"] == 0


@pytest.mark.parametrize("failure", ["invalid", "runtime"])
def test_invalid_final_and_runtime_failure_clear_uncached_previews(api, monkeypatch, failure):
    client, engine = api
    engine.supported_translation_previews = True
    source = "Wait for [[PLM_0]]."
    if failure == "invalid":
        engine.outputs[source] = "数式が欠けている"
    else:
        original = engine.translate

        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Synthetic model failure")

        monkeypatch.setattr(engine, "translate", broken)
    scheduler = client.app.state.translation_jobs
    identifier = client.post(BASE, json=payload(source), headers=HEADERS).json()["id"]
    assert engine.blocked.wait(2)
    next(iter(scheduler._inflight.values())).progress("未検証")
    assert scheduler.poll(identifier)["previews"]
    engine.release.set()
    result = terminal(client, identifier)
    assert result["previews"] == []
    assert result["state"] == ("completed" if failure == "invalid" else "failed")
    assert client.get("/api/cache").json()["entries"] == 0


def test_legacy_json_clients_do_not_pay_for_unused_draft_callbacks(api):
    client, engine = api
    engine.supported_translation_previews = True
    with ThreadPoolExecutor(max_workers=1) as callers:
        future = callers.submit(
            client.post, "/api/translate", json=payload("Wait for JSON."), headers=HEADERS
        )
        assert engine.blocked.wait(2)
        work = next(iter(client.app.state.translation_jobs._inflight.values()))
        assert work.progress is None
        engine.release.set()
        assert future.result(timeout=3).status_code == 200

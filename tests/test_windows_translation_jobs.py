"""Real Windows engine/job integration with synthetic SSE; no executable or weights."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.llama_backend import LlamaEngine

HEADERS = {"X-Plamo-Client": "userscript"}
JOBS = "/api/translation-jobs"


def eventually(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("The expected synthetic SSE transition did not occur")


class FakeRequest:
    def __init__(self, transport, cancelled):
        self.transport, self.cancelled = transport, cancelled
        self.closed = threading.Event()
        self.prompt = ""
        self.source = ""

    def check(self):
        if self.closed.is_set() or self.cancelled.is_set():
            raise InterruptedError("The synthetic owned request was cancelled")

    def post_json(self, path, payload):
        assert path == "/tokenize"
        content = payload["content"]
        if "<|plamo:bos|>" in content:
            self.prompt = content
            if "Second weighted" in content:
                self.transport.second_tokenized.set()
            count = 20000 if "weighted" in content else 100
        else:
            self.source = content
            count = 20
        return {"tokens": list(range(count))}

    def events(self, path, payload):
        assert path == "/completion" and payload["stream"] is True
        assert payload["cache_prompt"] is False
        text = self.prompt + self.source
        if "Held" in text:
            self.transport.held_started.set()
            yield {"content": "途中の訳文", "stop": False}
            while not self.transport.release.wait(0.005):
                if self.closed.is_set() or self.cancelled.is_set():
                    raise InterruptedError("The synthetic owned request was cancelled")
            if self.closed.is_set() or self.cancelled.is_set():
                raise InterruptedError("The synthetic owned request was cancelled")
            if "[[PLM_" in self.source:
                yield {"content": "<math-a/>。", "stop": False}
            else:
                yield {"content": "。", "stop": False}
        elif "Broken" in text:
            yield {"content": "壊れた<math-unknown/>。", "stop": False}
        else:
            if "Second weighted" in text:
                self.transport.second_started.set()
            yield {"content": "完成した訳文。", "stop": False}
        yield {"content": "", "stop": True, "stop_type": "eos", "truncated": False}

    def cancel(self):
        self.closed.set()


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.held_started = threading.Event()
        self.second_tokenized = threading.Event()
        self.second_started = threading.Event()
        self.release = threading.Event()

    def request(self, cancelled):
        result = FakeRequest(self, cancelled)
        self.requests.append(result)
        return result


@pytest.fixture
def windows_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {"system": "Windows", "machine": "AMD64", "memory_gb": 32, "supported": True},
    )
    engine = LlamaEngine(cache_dir=tmp_path / "unused-models")
    transport = FakeTransport()
    prepared = []

    def prepare(model, progress=None):
        prepared.append(model)
        engine._loaded = model

    monkeypatch.setattr(engine, "prepare", prepare)
    monkeypatch.setattr(engine, "model_available", lambda model: True)
    monkeypatch.setattr(engine, "_new_request", transport.request, raising=False)
    monkeypatch.setattr(
        engine, "_start", lambda *args, **kwargs: pytest.fail("No native process is allowed")
    )
    monkeypatch.setattr(
        engine, "download", lambda *args, **kwargs: pytest.fail("No download is allowed")
    )
    app = create_app(
        engine,
        cache_path=tmp_path / "translations.sqlite3",
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            yield client, engine, transport, prepared
        finally:
            transport.release.set()


def submit(client, text, **options):
    result = client.post(
        JOBS,
        json={
            "model": "2b",
            "batch_size": 32,
            "segments": [{"id": "same", "text": text}],
            **options,
        },
        headers=HEADERS,
    )
    assert result.status_code == 202, result.text
    return result.json()["id"]


def snapshot(client, identifier):
    result = client.get(f"{JOBS}/{identifier}", headers=HEADERS)
    assert result.status_code == 200, result.text
    return result.json()


def completed(client, identifier):
    def check():
        value = snapshot(client, identifier)
        return value if value["state"] == "completed" else None

    return eventually(check)


def test_windows_setup_advertises_common_capabilities_without_model_work(windows_jobs):
    client, engine, transport, prepared = windows_jobs
    state = client.get("/api/setup").json()
    assert state["device"]["system"] == "Windows"
    assert state["supported_batch_sizes"] == list(range(1, 33))
    assert state["supported_translation_jobs"] is True
    assert state["supported_translation_previews"] is True
    assert prepared == [] and transport.requests == []
    assert engine.status["loaded_model"] is None


def test_windows_sse_preview_is_uncached_and_cancelling_one_page_keeps_another(windows_jobs):
    client, engine, transport, _ = windows_jobs
    first = submit(client, "Held [[PLM_0]].")
    assert transport.held_started.wait(2)

    def preview():
        value = snapshot(client, first)
        return value if value["previews"] else None

    draft = eventually(preview)
    assert draft["events"] == [] and draft["completed"] == 0
    assert draft["previews"][0]["id"] == "same"
    assert "途中の訳文" in draft["previews"][0]["text"]
    assert client.get("/api/cache").json()["entries"] == 0
    second = submit(client, "Complete this other page.", glossary=[])
    other = completed(client, second)
    assert other["events"][0]["text"] == "完成した訳文。"
    assert other["events"][0]["id"] == "same"
    assert snapshot(client, first)["state"] == "running"
    cancelled = client.post(f"{JOBS}/{first}/cancel", json={}, headers=HEADERS)
    assert cancelled.status_code == 200
    assert snapshot(client, first)["previews"] == []
    assert snapshot(client, first)["events"] == []
    assert completed(client, second)["state"] == "completed"
    assert client.get("/api/cache").json()["entries"] == 1
    assert engine._process is None  # This test never starts or kills a native child.


def test_windows_verified_final_decodes_markers_and_only_then_caches(windows_jobs):
    client, _engine, transport, _ = windows_jobs
    identifier = submit(client, "Held [[PLM_0]].")
    assert transport.held_started.wait(2)
    assert client.get("/api/cache").json()["entries"] == 0
    transport.release.set()
    final = completed(client, identifier)
    assert final["previews"] == []
    assert final["events"] == [
        {
            "seq": 1,
            "type": "translation",
            "id": "same",
            "text": "途中の訳文[[PLM_0]]。",
            "cached": False,
        }
    ]
    assert client.get("/api/cache").json()["entries"] == 1
    previous_requests = len(transport.requests)
    cached = completed(client, submit(client, "Held [[PLM_0]]."))
    assert cached["cached"] == 1 and cached["events"][0]["cached"] is True
    assert len(transport.requests) == previous_requests


def test_windows_bad_final_is_isolated_and_never_cached(windows_jobs):
    client, _engine, _transport, _ = windows_jobs
    bad = submit(client, "Broken [[PLM_0]].")
    good = submit(client, "Complete this good paragraph.")
    failed_segment = completed(client, bad)
    assert failed_segment["events"][0]["type"] == "segment_error"
    assert failed_segment["previews"] == []
    assert completed(client, good)["events"][0]["text"] == "完成した訳文。"
    assert client.get("/api/cache").json()["entries"] == 1


def test_windows_shared_context_reservation_queues_a_second_large_prompt(windows_jobs):
    client, _engine, transport, _ = windows_jobs
    first = submit(client, "Held weighted paragraph.")
    assert transport.held_started.wait(2)
    second = submit(client, "Second weighted paragraph.")
    assert transport.second_tokenized.wait(2)
    assert not transport.second_started.is_set()
    assert snapshot(client, first)["state"] == "running"
    transport.release.set()
    assert completed(client, first)["state"] == "completed"
    assert completed(client, second)["state"] == "completed"
    assert transport.second_started.is_set()

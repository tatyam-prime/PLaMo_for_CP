"""The first-run UI cannot trigger model work until its download button is used."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.setup import SetupController


class FakeSetupEngine:
    model_specs: ClassVar[dict] = {
        "0.8b": {
            "name": "Small",
            "repo": "official/small",
            "revision": "small-v1",
            "weight_gb": 0.57,
            "min_memory_gb": 4,
        },
        "2b": {
            "name": "Normal",
            "repo": "official/normal",
            "revision": "normal-v1",
            "weight_gb": 1.81,
            "min_memory_gb": 8,
        },
        "8b": {
            "name": "Large",
            "repo": "official/large",
            "revision": "large-v1",
            "weight_gb": 5.28,
            "min_memory_gb": 16,
        },
        "31b": {
            "name": "Largest",
            "repo": "official/largest",
            "revision": "largest-v1",
            "weight_gb": 19.53,
            "min_memory_gb": 32,
        },
    }

    def __init__(self):
        self.status = {"backend": "fake", "loaded_model": None, "state": "idle"}
        self.available = set()
        self.prepared = []
        self.translated = []
        self.started = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()
        self.error = None

    def model_available(self, model):
        return model in self.available

    def download(self, model, progress, cancelled=None):
        progress({"stage": "downloading", "bytes_received": 1234})
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("中断")
        self.available.add(model)

    def prepare(self, model, progress):
        if model not in self.available:
            raise RuntimeError("先にダウンロードしてください。")
        self.prepared.append(model)
        self.started.set()
        progress({"stage": "loading"})
        if not self.proceed.wait(timeout=2):
            raise RuntimeError("test preparation timed out")
        if self.error:
            raise self.error
        progress({"stage": "loading"})
        self.available.add(model)
        self.status["loaded_model"] = model

    def translate(self, text, model, source_lang, target_lang, cancelled=None):
        self.translated.append(text)
        return "訳文: " + text


@pytest.fixture
def supported_device(monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {
            "system": "Windows",
            "machine": "AMD64",
            "memory_gb": 16,
            "supported": True,
        },
    )


@pytest.fixture
def controller(tmp_path, supported_device):
    engine = FakeSetupEngine()
    with ThreadPoolExecutor(max_workers=1) as executor:
        setup = SetupController(engine, executor, settings_path=tmp_path / "settings.json")
        yield setup, engine
        engine.proceed.set()
        setup.close()


def wait_finished(setup):
    setup._future.result(timeout=3)
    return setup.snapshot()


def test_first_run_and_status_never_prepare_or_download(controller):
    setup, engine = controller
    for _ in range(3):
        state = setup.snapshot()
        assert state["selected_model"] == "2b"
        assert state["preparation"]["stage"] == "idle"
        assert not any(model["available"] for model in state["models"])
    assert not engine.prepared
    assert not engine.translated


def test_explicit_button_runs_in_background_reports_progress_and_saves(controller, tmp_path):
    setup, engine = controller
    engine.available.add("0.8b")
    engine.proceed.clear()
    assert setup.start("0.8b")["stage"] == "queued"
    assert engine.started.wait(1)
    deadline = time.monotonic() + 1
    while setup.snapshot()["preparation"]["stage"] != "loading" and time.monotonic() < deadline:
        time.sleep(0.001)
    state = setup.snapshot()
    assert state["preparation"]["bytes_received"] == 0
    assert state["selected_model"] == "2b"
    engine.proceed.set()
    state = wait_finished(setup)
    assert state["preparation"]["stage"] == "ready"
    assert state["selected_model"] == "0.8b"
    assert json.loads((tmp_path / "settings.json").read_text()) == {"selected_model": "0.8b"}


def test_repeat_click_cannot_start_parallel_cached_preparations(controller):
    setup, engine = controller
    engine.available.update({"2b", "0.8b"})
    engine.proceed.clear()
    setup.start("2b")
    assert engine.started.wait(1)
    with pytest.raises(RuntimeError, match="準備"):
        setup.start("0.8b")
    assert engine.prepared == ["2b"]
    engine.proceed.set()
    wait_finished(setup)


def test_failed_prepare_preserves_previous_selection_and_allows_retry(controller):
    setup, engine = controller
    engine.available.add("0.8b")
    engine.error = RuntimeError("ネットワークが切れました。")
    setup.start("0.8b")
    state = wait_finished(setup)
    assert state["preparation"]["stage"] == "error"
    assert "ネットワーク" in state["preparation"]["error"]
    assert state["selected_model"] == "2b"
    engine.error = None
    setup.start("0.8b")
    assert wait_finished(setup)["selected_model"] == "0.8b"


def test_low_ram_and_unknown_model_never_prepare(controller, monkeypatch):
    setup, engine = controller
    monkeypatch.setattr("plamo_cp.setup.device_info", lambda: {"supported": True, "memory_gb": 8})
    with pytest.raises(ValueError, match="メモリ"):
        setup.start("8b")
    with pytest.raises(ValueError, match="モデル"):
        setup.start("31b")
    with pytest.raises(ValueError, match="モデル"):
        setup.start("made-up")
    assert engine.prepared == []
    compatibility = {model["id"]: model["compatible"] for model in setup.snapshot()["models"]}
    assert compatibility["2b"] is True
    assert compatibility["8b"] is False
    assert "31b" not in compatibility


def test_unsupported_device_never_prepares(controller, monkeypatch):
    setup, engine = controller
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {
            "system": "Darwin",
            "machine": "x86_64",
            "memory_gb": 32,
            "supported": False,
        },
    )
    with pytest.raises(RuntimeError, match="対応"):
        setup.start("2b")
    assert not engine.prepared


def test_cancellation_is_observed_by_next_progress(controller):
    setup, engine = controller
    engine.available.add("2b")
    engine.proceed.clear()
    setup.start("2b")
    assert engine.started.wait(1)
    setup.cancel()
    engine.proceed.set()
    state = wait_finished(setup)
    assert state["preparation"]["stage"] == "error"
    assert "中断" in state["preparation"]["error"]
    assert engine.available == {"2b"}
    assert engine.status["loaded_model"] is None


@pytest.mark.parametrize("selected", ["0.8b", "2b", "8b"])
def test_saved_selection_loads_without_preparation(tmp_path, supported_device, selected):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": selected}))
    engine = FakeSetupEngine()
    with ThreadPoolExecutor(max_workers=1) as executor:
        setup = SetupController(engine, executor, settings)
        assert setup.snapshot()["selected_model"] == selected
        assert not engine.prepared


@pytest.mark.parametrize("saved", ["bad json", "null", "[]", '{"selected_model": "wrong"}'])
def test_invalid_settings_use_safe_default_without_work(tmp_path, supported_device, saved):
    path = tmp_path / "settings.json"
    path.write_text(saved)
    engine = FakeSetupEngine()
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert SetupController(engine, executor, path).selected_model == "2b"
    assert not engine.prepared


def test_api_requires_explicit_prepare_and_does_not_translate_missing_model(
    tmp_path, supported_device
):
    engine = FakeSetupEngine()
    with TestClient(
        create_app(
            engine=engine,
            cache_path=tmp_path / "cache.sqlite",
            settings_path=tmp_path / "settings.json",
        ),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get("/api/setup").status_code == 200
        assert client.get("/api/models").status_code == 200
        assert not engine.prepared
        body = {"model": "2b", "segments": [{"id": "p", "text": "Hello."}]}
        response = client.post(
            "/api/translate", json=body, headers={"X-Plamo-Client": "userscript"}
        )
        assert response.status_code == 409
        assert not engine.prepared and not engine.translated
        assert client.post("/api/setup/prepare", json={"model": "2b"}).status_code == 403
        assert not engine.prepared
        response = client.post(
            "/api/setup/prepare", json={"model": "2b"}, headers={"X-Plamo-Client": "setup"}
        )
        assert response.status_code == 409
        assert (
            client.post(
                "/api/models/download", json={"model": "2b"}, headers={"X-Plamo-Client": "setup"}
            ).status_code
            == 202
        )
        client.app.state.setup._download_future.result(timeout=3)
        assert engine.prepared == []
        assert client.get("/api/setup").json()["download"]["stage"] == "ready"
        response = client.post(
            "/api/setup/prepare", json={"model": "2b"}, headers={"X-Plamo-Client": "setup"}
        )
        assert response.status_code == 202
        client.app.state.setup._future.result(timeout=3)
        assert client.get("/api/setup").json()["preparation"]["stage"] == "ready"
        assert (
            client.post(
                "/api/translate", json=body, headers={"X-Plamo-Client": "userscript"}
            ).status_code
            == 200
        )
        assert engine.prepared == ["2b"]
        assert engine.translated == ["Hello."]


def test_initial_glossary_snapshot_is_copied_and_never_starts_a_model(controller):
    setup, engine = controller
    first = setup.snapshot()["default_glossary"]
    assert first[0] == {"source": "modular inverse", "target": "逆元"}
    assert len(first) == 12
    first[0]["target"] = "changed"
    first.clear()
    second = setup.snapshot()["default_glossary"]
    assert len(second) == 12 and second[0]["target"] == "逆元"
    assert engine.prepared == [] and engine.translated == []


def test_streaming_preview_capability_only_follows_supporting_engine(controller):
    setup, engine = controller
    assert setup.snapshot()["supported_translation_previews"] is False
    engine.supported_translation_previews = True
    assert setup.snapshot()["supported_translation_previews"] is True


@pytest.mark.parametrize(
    "memory,expected", [(4, "0.8b"), (7.4, "0.8b"), (7.8, "2b"), (8, "2b"), (24, "2b"), (0, "2b")]
)
def test_fresh_model_recommendation_uses_2b_except_detected_low_memory(
    tmp_path, monkeypatch, memory, expected
):
    monkeypatch.setattr("plamo_cp.setup.device_info", lambda: {"memory_gb": memory})
    engine = FakeSetupEngine()
    settings = tmp_path / "settings.json"
    with ThreadPoolExecutor(max_workers=1) as executor:
        setup = SetupController(engine, executor, settings)
        assert setup.selected_model == expected
    assert not settings.exists() and engine.prepared == []


def test_low_memory_recommendation_never_overwrites_an_explicit_saved_model(tmp_path, monkeypatch):
    monkeypatch.setattr("plamo_cp.setup.device_info", lambda: {"memory_gb": 4})
    settings = tmp_path / "settings.json"
    saved = {"selected_model": "2b", "model_folder": "/existing/folder"}
    settings.write_text(json.dumps(saved))
    engine = FakeSetupEngine()
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert SetupController(engine, executor, settings).selected_model == "2b"
    assert json.loads(settings.read_text()) == saved and engine.prepared == []


def test_cached_selection_rechecks_inference_that_started_during_cache_inspection(
    tmp_path, supported_device, monkeypatch
):
    engine = FakeSetupEngine()
    engine.available.update({"0.8b", "2b"})
    app = create_app(engine, tmp_path / "cache.sqlite3", tmp_path / "settings.json")
    inspecting, release_inspection = threading.Event(), threading.Event()
    translating, release_translation = threading.Event(), threading.Event()
    available = app.state.setup.model_available

    def inspect(model):
        inspecting.set()
        assert release_inspection.wait(2)
        return available(model)

    def translate(text, *args, **kwargs):
        translating.set()
        assert release_translation.wait(2)
        return "訳文"

    monkeypatch.setattr(app.state.setup, "model_available", inspect)
    monkeypatch.setattr(engine, "translate", translate)
    with (
        TestClient(app, base_url="http://127.0.0.1") as client,
        ThreadPoolExecutor(max_workers=2) as callers,
    ):
        preparing = callers.submit(
            client.post,
            "/api/setup/prepare",
            json={"model": "0.8b"},
            headers={"X-Plamo-Client": "setup"},
        )
        assert inspecting.wait(1)
        inference = callers.submit(
            client.post,
            "/api/translate",
            json={"model": "2b", "segments": [{"id": "p", "text": "Original."}]},
            headers={"X-Plamo-Client": "userscript"},
        )
        try:
            assert translating.wait(1)
            release_inspection.set()
            assert preparing.result(timeout=2).status_code == 409
            assert engine.prepared == []
        finally:
            release_inspection.set()
            release_translation.set()
        assert inference.result(timeout=2).status_code == 200

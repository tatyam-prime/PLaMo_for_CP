"""Independent model I/O is tested with barriers, never weights or a GPU."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.engine import MODEL_SPECS
from plamo_cp.settings import SettingsStore

SETUP = {"X-Plamo-Client": "setup"}
SCRIPT = {"X-Plamo-Client": "userscript"}
DOWNLOAD = "/api/models/download"
JOBS = "/api/translation-jobs"


def eventually(check, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("The expected worker transition did not occur")


class BarrierEngine:
    supported_batch_sizes = (1,)

    def __init__(self):
        self.model_specs = {key: dict(MODEL_SPECS[key]) for key in ("0.8b", "2b", "8b")}
        self.status = {"backend": "fake", "state": "idle", "loaded_model": "2b"}
        self.available = {"0.8b", "2b"}
        self.downloads = []
        self.prepared = []
        self.translated = []
        self.download_started = threading.Event()
        self.download_release = threading.Event()
        self.download_exited = threading.Event()
        self.download_cancelled = threading.Event()
        self.translation_started = threading.Event()
        self.translation_release = threading.Event()
        self.translation_exited = threading.Event()
        self.translation_cancelled = threading.Event()
        self.download_error = None

    def model_available(self, model):
        return model in self.available

    def download(self, model, progress=None, cancelled=None):
        self.downloads.append((model, threading.get_ident()))
        self.download_exited.clear()
        self.download_started.set()
        deadline = time.monotonic() + 4
        try:
            while not self.download_release.wait(0.005):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("取得を停止しました。")
                if progress:
                    progress({"stage": "downloading", "bytes_received": 123})
                if time.monotonic() >= deadline:
                    raise RuntimeError("The fake download was not released")
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("取得を停止しました。")
            if progress:
                progress({"stage": "downloading", "bytes_received": 456})
            if self.download_error:
                raise self.download_error
            self.available.add(model)
        except InterruptedError:
            self.download_cancelled.set()
            raise
        finally:
            self.download_exited.set()

    def prepare(self, model, progress=None):
        assert model in self.available, "Cached-only loading must not fetch weights"
        self.prepared.append((model, threading.get_ident()))
        self.status["loaded_model"] = model

    def translate(self, text, *, model, cancelled=None, **options):
        self.translated.append((model, text, threading.get_ident()))
        self.status.update(state="translating", loaded_model=model)
        self.translation_started.set()
        try:
            if text.startswith("Wait"):
                deadline = time.monotonic() + 4
                while not self.translation_release.wait(0.005):
                    if cancelled is not None and cancelled.is_set():
                        self.translation_cancelled.set()
                        raise InterruptedError("翻訳を停止しました。")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The fake inference was not released")
            return "訳文: " + text
        finally:
            self.status["state"] = "idle"
            self.translation_exited.set()

    def unload(self, model=None):
        if model is None or self.status["loaded_model"] == model:
            self.status["loaded_model"] = None


class ForbiddenStorage:
    """A busy API must reject these operations before entering storage code."""

    def __init__(self, engine):
        self.model_specs = engine.model_specs

    def set_folder(self, folder):
        pytest.fail("A running download must protect its storage folder")

    def choose_folder(self):
        pytest.fail("A running download must not open a folder picker")

    def delete(self, model):
        pytest.fail("A running download must protect cached files")


def make_app(tmp_path, engine):
    path = tmp_path / "settings.json"
    SettingsStore(path).update(selected_model="2b")
    return create_app(
        engine,
        tmp_path / "translations.sqlite3",
        path,
        model_storage=ForbiddenStorage(engine),
    )


@pytest.fixture
def supported_device(monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {
            "system": "Darwin",
            "machine": "arm64",
            "memory_gb": 64,
            "supported": True,
        },
    )


@pytest.fixture
def api(tmp_path, supported_device):
    engine = BarrierEngine()
    app = make_app(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            yield client, engine
        finally:
            engine.download_release.set()
            engine.translation_release.set()


def download_state(client, expected=None):
    state = client.get("/api/setup").json()["download"]
    return state if expected is None or state["stage"] == expected else None


def start_download(client, engine):
    response = client.post(DOWNLOAD, json={"model": "8b"}, headers=SETUP)
    assert response.status_code == 202, response.text
    assert engine.download_started.wait(1)
    eventually(lambda: download_state(client, "downloading"))


def start_job(client, text="Find the answer.", model="2b"):
    return client.post(
        JOBS,
        json={"model": model, "segments": [{"id": "one", "text": text}]},
        headers=SCRIPT,
    )


def terminal_job(client, identifier):
    def check():
        response = client.get(f"{JOBS}/{identifier}", headers=SCRIPT)
        assert response.status_code == 200, response.text
        result = response.json()
        return result if result["state"] in {"completed", "cancelled", "failed"} else None

    return eventually(check)


def test_readonly_status_and_missing_prepare_never_start_a_download(api):
    client, engine = api
    for _ in range(3):
        assert client.get("/api/setup").status_code == 200
        assert client.get("/api/health").status_code == 200
    response = client.post("/api/setup/prepare", json={"model": "8b"}, headers=SETUP)
    assert response.status_code == 409, response.text
    assert not engine.downloads and not engine.prepared
    assert engine.status["loaded_model"] == "2b"


def test_download_and_cached_translation_share_no_worker_or_model_selection(api):
    client, engine = api
    waiting = start_job(client, "Wait for the active translation.")
    assert waiting.status_code == 202, waiting.text
    assert engine.translation_started.wait(1)
    start_download(client, engine)
    assert engine.status == {"backend": "fake", "state": "translating", "loaded_model": "2b"}
    assert download_state(client)["bytes_received"] == 123
    assert engine.downloads[0][1] != engine.translated[0][2]
    assert start_job(client, model="8b").status_code == 409
    engine.translation_release.set()
    assert terminal_job(client, waiting.json()["id"])["state"] == "completed"
    second = start_job(client, "Translate with another cached model.", model="0.8b")
    assert second.status_code == 202, second.text
    assert terminal_job(client, second.json()["id"])["state"] == "completed"
    assert not engine.download_release.is_set()
    loaded = client.post("/api/setup/prepare", json={"model": "0.8b"}, headers=SETUP)
    assert loaded.status_code == 202, loaded.text
    eventually(lambda: client.get("/api/setup").json()["preparation"]["stage"] == "ready")
    assert engine.prepared[0][1] != engine.downloads[0][1]
    assert client.get("/api/setup").json()["selected_model"] == "0.8b"
    engine.download_release.set()
    assert eventually(lambda: download_state(client, "ready"))["model"] == "8b"
    assert "8b" in engine.available
    assert engine.status["loaded_model"] == "0.8b"
    assert client.get("/api/setup").json()["selected_model"] == "0.8b"
    assert [model for model, _ in engine.prepared] == ["0.8b"]
    final = start_job(client, "Use the newly downloaded model.", model="8b")
    assert final.status_code == 202, final.text
    assert terminal_job(client, final.json()["id"])["state"] == "completed"


def test_one_download_protects_folder_and_all_model_deletions(api, tmp_path):
    client, engine = api
    start_download(client, engine)
    operations = [
        (DOWNLOAD, {"model": "2b"}),
        ("/api/models/folder", {"folder": str(tmp_path / "new-folder")}),
        ("/api/models/choose-folder", {}),
        ("/api/models/delete", {"model": "8b"}),
        ("/api/models/delete", {"model": "0.8b"}),
    ]
    for path, body in operations:
        response = client.post(path, json=body, headers=SETUP)
        assert response.status_code == 409, (path, response.text)
    assert len(engine.downloads) == 1
    assert not (tmp_path / "new-folder").exists()


def test_cached_memory_can_be_released_without_cancelling_a_download(api):
    client, engine = api
    start_download(client, engine)
    response = client.post("/api/models/unload", json={"model": "2b"}, headers=SETUP)
    assert response.status_code == 200, response.text
    assert engine.status["loaded_model"] is None
    assert download_state(client)["stage"] == "downloading"
    engine.download_release.set()
    eventually(lambda: download_state(client, "ready"))
    assert engine.status["loaded_model"] is None
    assert client.get("/api/setup").json()["selected_model"] == "2b"
    assert not engine.prepared


def test_unloading_the_transfer_target_cannot_forget_an_active_download(api):
    client, engine = api
    start_download(client, engine)
    response = client.post("/api/models/unload", json={"model": "8b"}, headers=SETUP)
    assert response.status_code in {200, 409}, response.text
    state = download_state(client)
    assert state["model"] == "8b" and state["stage"] == "downloading"
    assert start_job(client, model="8b").status_code == 409
    assert not engine.download_cancelled.is_set()


@pytest.mark.parametrize("path,body", [(DOWNLOAD, {"model": "8b"}), (DOWNLOAD + "/cancel", {})])
def test_download_controls_require_the_management_header(api, path, body):
    client, engine = api
    for headers in ({}, SCRIPT):
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 403, response.text
    assert not engine.downloads


def test_failed_download_preserves_loaded_model_and_can_be_retried(api):
    client, engine = api
    engine.download_error = RuntimeError("fake network failure")
    engine.download_release.set()
    response = client.post(DOWNLOAD, json={"model": "8b"}, headers=SETUP)
    assert response.status_code == 202
    state = eventually(lambda: download_state(client, "error"))
    assert "fake network failure" in state["error"]
    assert "8b" not in engine.available and engine.status["loaded_model"] == "2b"
    assert client.get("/api/setup").json()["selected_model"] == "2b"
    engine.download_error = None
    engine.download_started.clear()
    engine.download_release.clear()
    start_download(client, engine)
    engine.download_release.set()
    eventually(lambda: download_state(client, "ready"))
    assert len(engine.downloads) == 2 and not engine.prepared
    assert engine.status["loaded_model"] == "2b"


def test_cancelling_download_does_not_cancel_active_translation(api):
    client, engine = api
    job = start_job(client, "Wait while download is cancelled.")
    assert job.status_code == 202 and engine.translation_started.wait(1)
    start_download(client, engine)
    response = client.post(DOWNLOAD + "/cancel", json={}, headers=SETUP)
    assert response.status_code == 200, response.text
    assert engine.download_cancelled.wait(1)
    eventually(lambda: download_state(client, "cancelled"))
    assert "8b" not in engine.available
    assert not engine.translation_cancelled.is_set()
    assert client.get(f"{JOBS}/{job.json()['id']}", headers=SCRIPT).json()["state"] == "running"
    engine.translation_release.set()
    assert terminal_job(client, job.json()["id"])["state"] == "completed"
    engine.download_started.clear()
    start_download(client, engine)
    engine.download_release.set()
    eventually(lambda: download_state(client, "ready"))
    assert len(engine.downloads) == 2


def test_cancelling_translation_does_not_cancel_download(api):
    client, engine = api
    start_download(client, engine)
    job = start_job(client, "Wait until cancelled.")
    assert job.status_code == 202 and engine.translation_started.wait(1)
    assert (
        client.post(f"{JOBS}/{job.json()['id']}/cancel", json={}, headers=SCRIPT).status_code == 200
    )
    assert engine.translation_cancelled.wait(1)
    assert not engine.download_cancelled.is_set()
    assert download_state(client)["stage"] == "downloading"
    engine.download_release.set()
    eventually(lambda: download_state(client, "ready"))
    assert "8b" in engine.available


def test_shutdown_cooperatively_cancels_a_blocked_download(tmp_path, supported_device):
    engine = BarrierEngine()
    app = make_app(tmp_path, engine)
    started = None
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            start_download(client, engine)
            started = time.monotonic()
        assert time.monotonic() - started < 2
        assert engine.download_cancelled.is_set() and engine.download_exited.is_set()
        assert "8b" not in engine.available
    finally:
        engine.download_release.set()
        engine.translation_release.set()

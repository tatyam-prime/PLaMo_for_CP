"""Folder settings and API tests use tiny temporary files, without model inference."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.engine import MODEL_SPECS
from plamo_cp.settings import SettingsStore

SETUP = {"X-Plamo-Client": "setup"}
USERSCRIPT = {"X-Plamo-Client": "userscript"}


class FolderEngine:
    def __init__(self, folder):
        self.model_specs = {"2b": {**MODEL_SPECS["2b"], "filename": "tiny.gguf", "weight_bytes": 3}}
        self.cache_dir = Path(folder).resolve()
        self.status = {"state": "idle", "loaded_model": None, "backend": "test"}
        self.prepared = []

    def set_cache_dir(self, folder):
        self.cache_dir = Path(folder)
        self.status["loaded_model"] = None

    def _model_path(self):
        spec = self.model_specs["2b"]
        return (
            self.cache_dir
            / ("models--" + spec["repo"].replace("/", "--"))
            / "snapshots"
            / spec["revision"]
            / spec["filename"]
        )

    def model_available(self, model):
        return model == "2b" and self._model_path().is_file()

    def download(self, model, progress=None, cancelled=None):
        path = self._model_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"123")
        return str(path)

    def prepare(self, model, progress=None):
        self.prepared.append(self.cache_dir)
        path = self._model_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"123")
        self.status["loaded_model"] = model

    def translate(self, text, **kwargs):
        return "訳文"

    def unload(self, model=None):
        if model is None or self.status["loaded_model"] == model:
            self.status["loaded_model"] = None


def app_for(tmp_path, engine):
    return create_app(
        engine=engine,
        cache_path=tmp_path / "translations.sqlite3",
        settings_path=tmp_path / "settings.json",
    )


def test_folder_api_persists_and_new_preparations_use_only_the_chosen_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {"supported": True, "memory_gb": 64, "system": "Darwin"},
    )
    old, new = tmp_path / "old", tmp_path / "選択したフォルダー"
    engine = FolderEngine(old)
    engine.prepare("2b")
    old_file = engine._model_path()
    SettingsStore(tmp_path / "settings.json").update(selected_model="2b", extra="keep")
    app = app_for(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.setup._state.update(stage="ready", model="2b")
        response = client.post("/api/models/folder", json={"folder": str(new)}, headers=SETUP)
        assert response.status_code == 200, response.text
        assert "選択したフォルダー" in response.text
        assert (
            response.json()["folder"] == str(new.resolve()) and response.json()["shared"] is False
        )
        assert not response.json()["models"][0]["downloaded"]
        assert old_file.read_bytes() == b"123" and engine.cache_dir == new.resolve()
        assert client.get("/api/setup").json()["preparation"]["stage"] == "idle"
        assert (
            client.post("/api/setup/prepare", json={"model": "2b"}, headers=SETUP).status_code
            == 409
        )
        assert (
            client.post("/api/models/download", json={"model": "2b"}, headers=SETUP).status_code
            == 202
        )
        app.state.setup._download_future.result(timeout=2)
        assert engine.prepared == [old.resolve()]
        assert (
            client.post("/api/setup/prepare", json={"model": "2b"}, headers=SETUP).status_code
            == 202
        )
        app.state.setup._future.result(timeout=2)
        assert engine._model_path().read_bytes() == b"123"
        assert engine.prepared == [old.resolve(), new.resolve()]
    saved = SettingsStore(tmp_path / "settings.json").read()
    assert saved == {"selected_model": "2b", "model_folder": str(new.resolve()), "extra": "keep"}
    restarted = FolderEngine(old)
    with TestClient(app_for(tmp_path, restarted), base_url="http://127.0.0.1") as client:
        assert client.get("/api/models/storage").json()["folder"] == str(new.resolve())
        assert client.get("/api/setup").json()["models"][0]["available"] is True
        assert restarted.cache_dir == new.resolve() and old_file.is_file()


def test_native_folder_api_cancel_and_success_are_readable_and_never_open_real_picker(
    tmp_path, monkeypatch
):
    from plamo_cp import folder_picker

    old, new = tmp_path / "old", tmp_path / "selected"
    app = app_for(tmp_path, FolderEngine(old))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.setup._state.update(stage="ready", model="2b")
        monkeypatch.setattr(folder_picker, "choose_folder", lambda initial: None)
        assert client.post("/api/models/choose-folder", json={}, headers=SETUP).json() == {
            "cancelled": True
        }
        assert app.state.setup.snapshot()["preparation"]["stage"] == "ready"
        assert not (tmp_path / "settings.json").exists()
        monkeypatch.setattr(folder_picker, "choose_folder", lambda initial: new)
        result = client.post("/api/models/choose-folder", json={}, headers=SETUP)
        assert result.status_code == 200 and result.json()["folder"] == str(new.resolve())
        assert app.state.setup.snapshot()["preparation"]["stage"] == "idle"


@pytest.mark.parametrize(
    "endpoint,body", [("choose-folder", {}), ("folder", {"folder": "/unused"})]
)
def test_folder_endpoints_require_the_management_header(tmp_path, monkeypatch, endpoint, body):
    from plamo_cp import folder_picker

    monkeypatch.setattr(
        folder_picker, "choose_folder", lambda _: pytest.fail("unauthorized picker")
    )
    with TestClient(
        app_for(tmp_path, FolderEngine(tmp_path / "old")), base_url="http://127.0.0.1"
    ) as client:
        assert client.post("/api/models/" + endpoint, json=body).status_code == 403
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize("busy", ["translating", "preparing"])
def test_folder_change_rejects_busy_work_before_showing_a_picker(tmp_path, monkeypatch, busy):
    from plamo_cp import folder_picker

    monkeypatch.setattr(folder_picker, "choose_folder", lambda _: pytest.fail("busy picker"))
    engine = FolderEngine(tmp_path / "old")
    app = app_for(tmp_path, engine)
    if busy == "translating":
        engine.status["state"] = "translating"
    else:
        future = threading.Event()
        app.state.setup._future = type("Pending", (), {"done": lambda self: future.is_set()})()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/models/choose-folder", json={}, headers=SETUP).status_code == 409
        assert (
            client.post(
                "/api/models/folder", json={"folder": str(tmp_path / "new")}, headers=SETUP
            ).status_code
            == 409
        )
    assert not (tmp_path / "new").exists()


def test_pending_picker_blocks_another_picker_prepare_delete_and_translation(tmp_path, monkeypatch):
    from plamo_cp import folder_picker

    entered, release = threading.Event(), threading.Event()

    def choose(initial):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(folder_picker, "choose_folder", choose)
    with (
        TestClient(
            app_for(tmp_path, FolderEngine(tmp_path / "old")), base_url="http://127.0.0.1"
        ) as client,
        ThreadPoolExecutor() as workers,
    ):
        pending = workers.submit(client.post, "/api/models/choose-folder", json={}, headers=SETUP)
        assert entered.wait(2)
        try:
            for path, body, headers in [
                ("/api/models/choose-folder", {}, SETUP),
                ("/api/models/folder", {"folder": str(tmp_path / "other")}, SETUP),
                ("/api/models/delete", {"model": "2b"}, SETUP),
                ("/api/setup/prepare", {"model": "2b"}, SETUP),
                ("/api/translate", {"segments": [{"id": "p", "text": "Text"}]}, USERSCRIPT),
            ]:
                assert client.post(path, json=body, headers=headers).status_code == 409, path
        finally:
            release.set()
        assert pending.result(timeout=2).json() == {"cancelled": True}


def test_invalid_path_and_unavailable_picker_return_actionable_errors(tmp_path, monkeypatch):
    from plamo_cp import folder_picker

    def unavailable(initial):
        raise RuntimeError("保存先の選択画面を開けませんでした。")

    monkeypatch.setattr(folder_picker, "choose_folder", unavailable)
    with TestClient(
        app_for(tmp_path, FolderEngine(tmp_path / "old")), base_url="http://127.0.0.1"
    ) as client:
        assert (
            client.post(
                "/api/models/folder", json={"folder": "relative"}, headers=SETUP
            ).status_code
            == 422
        )
        result = client.post("/api/models/choose-folder", json={}, headers=SETUP)
        assert result.status_code == 503 and "選択画面" in result.json()["detail"]
    assert not (tmp_path / "settings.json").exists()


def test_mlx_availability_and_weight_downloads_use_explicit_folder_without_global_changes(
    tmp_path, monkeypatch
):
    from huggingface_hub import constants

    from plamo_cp import download
    from plamo_cp import engine as backend

    chosen = tmp_path / "chosen"
    shared = tmp_path / "global"
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(shared))
    spec = backend.MODEL_SPECS["2b"]
    paths = []
    for root in (chosen, shared):
        path = (
            root / ("models--" + spec["repo"].replace("/", "--")) / "snapshots" / spec["revision"]
        )
        path.mkdir(parents=True)
        for filename in (
            "config.json",
            "generation_config.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "tokenizer.jsonl",
            "chat_template.jinja",
            "modeling_plamo_mlx.py",
            "tokenization_plamo.py",
            "model.safetensors",
        ):
            (path / filename).write_text("cached", encoding="utf-8")
        paths.append(path)
    engine = backend.Engine(cache_dir=chosen)
    assert engine.model_available("2b") is True
    (paths[0] / "model.safetensors").unlink()
    assert engine.model_available("2b") is False
    assert (paths[1] / "model.safetensors").is_file()
    calls = []
    monkeypatch.setattr(
        download, "download_snapshot", lambda spec, **options: calls.append(options) or "local"
    )
    assert backend.download_model("2b", cache_dir=chosen) == "local"
    assert calls[0]["cache_dir"] == chosen.resolve()
    assert constants.HF_HUB_CACHE == str(shared)


def test_windows_checks_and_downloads_use_the_selected_model_folder(tmp_path, monkeypatch):
    from huggingface_hub import constants

    from plamo_cp import download
    from plamo_cp import llama_backend as backend

    chosen, shared = tmp_path / "chosen", tmp_path / "global"
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(shared))
    engine = backend.LlamaEngine(cache_dir=chosen)
    spec = {**backend.GGUF_MODELS["2b"], "weight_bytes": 3}
    engine.model_specs = {"2b": spec}
    path = chosen / ("models--" + spec["repo"].replace("/", "--")) / "snapshots" / spec["revision"]
    path.mkdir(parents=True)
    weight = path / spec["filename"]
    weight.write_bytes(b"123")
    monkeypatch.setattr(backend, "_runtime_candidates", lambda: [tmp_path / "runtime"])
    monkeypatch.setattr(
        backend, "_runtime_executable", lambda *_, **__: tmp_path / "llama-server.exe"
    )
    assert engine.model_available("2b") is True
    assert engine._model_path("2b") == weight
    weight.unlink()
    calls = []

    def fetch(spec, **options):
        calls.append(options)
        weight.write_bytes(b"123")
        return str(path)

    monkeypatch.setattr(download, "download_snapshot", fetch)
    monkeypatch.setattr(backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(backend, "windows_memory_gb", lambda: 64)
    monkeypatch.setattr(backend, "ensure_runtime", lambda *_, **__: tmp_path / "llama-server.exe")
    monkeypatch.setattr(backend, "_sha256", lambda _, **__: spec["sha256"])
    started = []
    monkeypatch.setattr(
        engine, "_start", lambda binary, model_path, model, progress: started.append(model_path)
    )
    try:
        engine.download("2b")
        assert started == []
        engine.prepare("2b")
        assert calls[0]["cache_dir"] == chosen.resolve()
        assert started == [weight]
        assert constants.HF_HUB_CACHE == str(shared)
    finally:
        engine.close()


def test_standalone_backends_restore_saved_destination_without_creating_it(tmp_path, monkeypatch):
    from plamo_cp import settings
    from plamo_cp.engine import Engine
    from plamo_cp.llama_backend import LlamaEngine

    selected = tmp_path / "未作成の保存先"
    settings_path = tmp_path / "settings.json"
    SettingsStore(settings_path).update(model_folder=str(selected))
    monkeypatch.setattr(settings, "DEFAULT_SETTINGS_PATH", settings_path)
    mlx, llama = Engine(), LlamaEngine()
    try:
        assert mlx.cache_dir == selected.resolve() and llama.cache_dir == selected.resolve()
        assert not selected.exists()
    finally:
        llama.close()


def test_unload_endpoint_releases_live_model_without_deleting_downloaded_files(tmp_path):
    engine = FolderEngine(tmp_path / "models")
    engine.prepare("2b")
    app = app_for(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.setup._state.update(stage="ready", model="2b")
        assert client.post("/api/models/unload", json={"model": "2b"}).status_code == 403
        engine.status["state"] = "translating"
        assert (
            client.post("/api/models/unload", json={"model": "2b"}, headers=SETUP).status_code
            == 409
        )
        engine.status["state"] = "idle"
        result = client.post("/api/models/unload", json={"model": "2b"}, headers=SETUP)
        assert result.status_code == 200 and result.json() == {"unloaded": True}
        assert engine.status["loaded_model"] is None and engine._model_path().read_bytes() == b"123"
        assert client.get("/api/setup").json()["preparation"]["stage"] == "idle"
        assert client.get("/api/models/storage").json()["models"][0]["downloaded"] is True


def test_closing_the_request_does_not_release_a_running_picker(tmp_path, monkeypatch):
    from plamo_cp import folder_picker

    entered, release = threading.Event(), threading.Event()

    def choose(initial):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(folder_picker, "choose_folder", choose)
    app = app_for(tmp_path, FolderEngine(tmp_path / "old"))

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client,
        ):
            pending = asyncio.create_task(
                client.post("/api/models/choose-folder", json={}, headers=SETUP)
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert (
                    await client.post(
                        "/api/models/folder",
                        json={"folder": str(tmp_path / "other")},
                        headers=SETUP,
                    )
                ).status_code == 409
            finally:
                release.set()
            for _ in range(100):
                result = await client.post(
                    "/api/models/folder", json={"folder": str(tmp_path / "other")}, headers=SETUP
                )
                if result.status_code == 200:
                    break
                await asyncio.sleep(0.001)
            assert result.status_code == 200

    asyncio.run(scenario())


def test_pending_translation_blocks_folder_change_even_before_engine_marks_busy(tmp_path):
    engine = FolderEngine(tmp_path / "models")
    engine.prepare("2b")
    entered, release = threading.Event(), threading.Event()

    def translate(text, **kwargs):
        entered.set()
        assert release.wait(3)
        return "訳文"

    engine.translate = translate
    with (
        TestClient(app_for(tmp_path, engine), base_url="http://127.0.0.1") as client,
        ThreadPoolExecutor() as workers,
    ):
        pending = workers.submit(
            client.post,
            "/api/translate",
            json={"model": "2b", "segments": [{"id": "p", "text": "Text"}]},
            headers=USERSCRIPT,
        )
        try:
            assert entered.wait(2)
            assert engine.status["state"] == "idle"
            assert (
                client.post(
                    "/api/models/folder", json={"folder": str(tmp_path / "new")}, headers=SETUP
                ).status_code
                == 409
            )
        finally:
            release.set()
        assert pending.result(timeout=2).status_code == 200


def test_saved_31b_falls_back_to_default_2b_but_legacy_weights_remain_manageable(
    tmp_path, monkeypatch
):
    from plamo_cp.engine import Engine

    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {"system": "Darwin", "machine": "arm64", "memory_gb": 16, "supported": True},
    )
    folder = tmp_path / "models"
    spec = MODEL_SPECS["31b"]
    legacy = folder / ("models--" + spec["repo"].replace("/", "--")) / "blobs" / "tiny.incomplete"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"keep")
    SettingsStore(tmp_path / "settings.json").update(selected_model="31b", model_folder=str(folder))
    engine = Engine(cache_dir=folder)
    with TestClient(app_for(tmp_path, engine), base_url="http://127.0.0.1") as client:
        setup = client.get("/api/setup").json()
        assert setup["selected_model"] == "2b"
        assert {item["id"] for item in setup["models"]} == {"0.8b", "2b", "8b"}
        assert legacy.read_bytes() == b"keep"
        storage = client.get("/api/models/storage").json()
        assert next(item for item in storage["models"] if item["id"] == "31b")["on_disk_bytes"] == 4
        assert engine.model_available("31b") is False
        result = client.post("/api/models/delete", json={"model": "31b"}, headers=SETUP)
        assert result.status_code == 200 and result.json()["deleted"] is True
    assert not legacy.exists()


def test_backends_reject_31b_before_any_platform_or_model_work(tmp_path, monkeypatch):
    from plamo_cp import download
    from plamo_cp.engine import Engine, download_model
    from plamo_cp.llama_backend import LlamaEngine

    monkeypatch.setattr(download, "download_snapshot", lambda *_: pytest.fail("no 31b download"))
    with pytest.raises(ValueError, match="モデルサイズ"):
        download_model("31b", cache_dir=tmp_path)
    engines = [Engine(cache_dir=tmp_path), LlamaEngine(cache_dir=tmp_path)]
    try:
        for engine in engines:
            with pytest.raises(ValueError, match="モデル"):
                engine.prepare("31b")
            with pytest.raises((ValueError, RuntimeError), match="モデル|準備"):
                engine.translate("Text.", model="31b")
            assert not engine.model_available("31b")
    finally:
        engines[1].close()

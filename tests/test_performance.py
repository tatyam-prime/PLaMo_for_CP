"""Quiet-mode settings and HTTP changes use only temporary files and fake runtimes."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.performance import PerformanceSettings
from plamo_cp.settings import SettingsStore

SETUP = {"X-Plamo-Client": "setup"}
SCRIPT = {"X-Plamo-Client": "userscript"}


class FakeRuntime:
    def __init__(self):
        self.status = {"backend": "fake", "state": "idle", "loaded_model": "8b"}
        self.quiet_level = 0
        self.applied = []
        self.failure = None
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def quiet(self):
        return self.quiet_level > 0

    def set_quiet_level(self, level):
        self.applied.append((level, threading.get_ident()))
        if self.failure:
            raise self.failure
        self.quiet_level = level

    def model_available(self, model):
        return True

    def translate(self, text, *, cancelled=None, **options):
        self.status["state"] = "translating"
        self.started.set()
        try:
            if text == "Wait for a runtime switch.":
                for _ in range(400):
                    if self.release.wait(0.005):
                        break
                    if cancelled is not None and cancelled.is_set():
                        raise InterruptedError("停止")
                else:
                    raise RuntimeError("The fake translation was not released")
            return "日本語: " + text
        finally:
            self.status["state"] = "idle"

    def prepare(self, *args, **kwargs):
        pytest.fail("A performance setting must not load a model")

    def download(self, *args, **kwargs):
        pytest.fail("A performance setting must not download a model")


@pytest.fixture
def app_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.setup.device_info",
        lambda: {"system": "Darwin", "machine": "arm64", "memory_gb": 32, "supported": True},
    )
    runtime = FakeRuntime()
    settings = SettingsStore(tmp_path / "settings.json")

    def make():
        return create_app(
            runtime,
            cache_path=tmp_path / "cache.sqlite3",
            settings_path=settings.path,
        )

    yield runtime, settings, make
    runtime.release.set()


def test_fresh_default_is_false_and_readonly_without_runtime_model_work(app_parts):
    runtime, settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert client.get("/api/setup").json()["quiet_mode"] is False
        assert client.get("/api/setup").json()["quiet_level"] == 0
        assert client.get("/api/health").json()["engine"]["loaded_model"] == "8b"
    assert runtime.applied == []
    assert not settings.path.exists()


def test_live_toggle_persists_only_its_key_and_is_restored_on_restart(app_parts, tmp_path):
    runtime, settings, make = app_parts
    other = {"selected_model": "8b", "model_folder": str(tmp_path / "モデル"), "unknown": [1]}
    settings.update(**other)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        response = client.post("/api/performance", json={"quiet_mode": True}, headers=SETUP)
        assert response.status_code == 200 and response.json() == {
            "quiet_level": 3,
            "quiet_mode": True,
        }
        assert client.get("/api/setup").json()["quiet_mode"] is True
        assert runtime.status["loaded_model"] == "8b"
        assert settings.read() == {**other, "quiet_level": 3, "quiet_mode": True}
    runtime.quiet_level = 0
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert runtime.quiet
        assert client.get("/api/setup").json()["selected_model"] == "8b"
        assert client.get("/api/setup").json()["quiet_mode"] is True
        response = client.post("/api/performance", json={"quiet_mode": False}, headers=SETUP)
        assert response.json() == {"quiet_level": 0, "quiet_mode": False}
    assert settings.read() == {**other, "quiet_level": 0, "quiet_mode": False}
    assert [level for level, _ in runtime.applied] == [3, 3, 0]


@pytest.mark.parametrize("stored", ["true", 1])
def test_invalid_saved_value_is_not_truthy_enabled(app_parts, stored):
    runtime, settings, make = app_parts
    settings.update(quiet_mode=stored)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert client.get("/api/setup").json()["quiet_mode"] is False
    assert not runtime.applied
    assert settings.read() == {"quiet_mode": stored}


@pytest.mark.parametrize("value", [1, "true", None])
def test_request_boolean_is_strict_before_applying_or_saving(app_parts, value):
    runtime, settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        response = client.post("/api/performance", json={"quiet_mode": value}, headers=SETUP)
        assert response.status_code == 422
    assert not runtime.applied and not settings.path.exists()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        SCRIPT,
        {**SETUP, "Origin": "https://example.org"},
        {**SCRIPT, "Origin": "https://example.org"},
    ],
)
def test_only_local_management_transport_can_change_performance(app_parts, headers):
    runtime, settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        result = client.post("/api/performance", json={"quiet_mode": True}, headers=headers)
        assert result.status_code == 403
        assert "access-control-allow-origin" not in result.headers
    assert runtime.applied == [] and not settings.path.exists()


def test_native_origin_is_allowed_but_foreign_preflight_is_not(app_parts):
    runtime, _settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        result = client.options(
            "/api/performance",
            headers={
                "Origin": "https://example.org",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-plamo-client,content-type",
            },
        )
        assert result.status_code == 403
        assert "access-control-allow-origin" not in result.headers
        result = client.post(
            "/api/performance",
            json={"quiet_mode": True},
            headers={**SETUP, "Origin": "http://127.0.0.1"},
        )
        assert result.status_code == 200 and runtime.quiet


def test_actual_apply_failure_is_503_and_never_saved(app_parts):
    runtime, settings, make = app_parts
    settings.update(selected_model="8b", quiet_mode=False)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        runtime.failure = RuntimeError("Windows エラー 50: CPU 制限を設定できません。")
        response = client.post("/api/performance", json={"quiet_mode": True}, headers=SETUP)
        assert response.status_code == 503 and "エラー 50" in response.json()["detail"]
        assert client.get("/api/setup").json()["quiet_mode"] is False
    assert not runtime.quiet
    assert settings.read() == {"selected_model": "8b", "quiet_mode": False}


def test_failed_disable_keeps_the_saved_and_effective_setting_enabled(app_parts):
    runtime, settings, make = app_parts
    settings.update(selected_model="8b", quiet_mode=True)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert runtime.quiet
        runtime.failure = RuntimeError("CPU 制限を解除できません。")
        response = client.post("/api/performance", json={"quiet_mode": False}, headers=SETUP)
        assert response.status_code == 503
        assert client.get("/api/setup").json()["quiet_mode"] is True
    assert runtime.quiet
    assert settings.read() == {"selected_model": "8b", "quiet_mode": True}


def test_switch_is_not_queued_behind_an_active_translation(app_parts):
    runtime, settings, make = app_parts
    settings.update(selected_model="8b")
    with (
        TestClient(make(), base_url="http://127.0.0.1") as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        translation = pool.submit(
            client.post,
            "/api/translate",
            json={
                "model": "8b",
                "segments": [{"id": "p", "text": "Wait for a runtime switch."}],
            },
            headers=SCRIPT,
        )
        try:
            assert runtime.started.wait(1)
            assert runtime.status["state"] == "translating"
            response = client.post("/api/performance", json={"quiet_mode": True}, headers=SETUP)
            assert response.status_code == 200 and runtime.quiet
            assert not translation.done()
            assert runtime.status["loaded_model"] == "8b"
            assert settings.read() == {"selected_model": "8b", "quiet_level": 3, "quiet_mode": True}
        finally:
            runtime.release.set()
        assert translation.result(timeout=2).status_code == 200


def test_failed_persistence_rolls_back_runtime_and_leaves_file_unchanged(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    store = SettingsStore(tmp_path / "settings.json")
    store.update(selected_model="8b")
    performance = PerformanceSettings(runtime, store)

    def unavailable(**changes):
        raise OSError("read-only test settings")

    monkeypatch.setattr(store, "update", unavailable)
    with pytest.raises(RuntimeError, match="保存できません"):
        performance.set_quiet_mode(True)
    assert not runtime.quiet and not performance.quiet_mode
    assert [level for level, _ in runtime.applied] == [3, 0]
    assert store.read() == {"selected_model": "8b"}


def test_failed_persistence_and_rollback_exposes_actual_applied_state(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    store = SettingsStore(tmp_path / "settings.json")
    performance = PerformanceSettings(runtime, store)

    def unavailable(**changes):
        runtime.failure = RuntimeError("mock rollback failure")
        raise OSError("read-only test settings")

    monkeypatch.setattr(store, "update", unavailable)
    with pytest.raises(RuntimeError, match="元に戻せません"):
        performance.set_quiet_mode(True)
    assert runtime.quiet and performance.quiet_mode
    assert not store.path.exists()


def test_concurrent_changes_apply_and_save_in_the_same_order(tmp_path):
    runtime = FakeRuntime()
    entered, release = threading.Event(), threading.Event()
    store = SettingsStore(tmp_path / "settings.json")
    first = True
    original_update = store.update

    def held_save(**changes):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(2)
        return original_update(**changes)

    store.update = held_save
    performance = PerformanceSettings(runtime, store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        enable = pool.submit(performance.set_quiet_mode, True)
        assert entered.wait(1)
        disable = pool.submit(performance.set_quiet_mode, False)
        assert [level for level, _ in runtime.applied] == [3]
        release.set()
        assert enable.result(timeout=2) == {"quiet_level": 3, "quiet_mode": True}
        assert disable.result(timeout=2) == {"quiet_level": 0, "quiet_mode": False}
    assert not performance.quiet_mode and not runtime.quiet
    assert store.read() == {"quiet_level": 0, "quiet_mode": False}


@pytest.mark.parametrize("level", [0, 3, 7])
def test_representative_presets_apply_and_persist_exact_levels(app_parts, level):
    runtime, settings, make = app_parts
    settings.update(selected_model="8b")
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        response = client.post("/api/performance", json={"quiet_level": level}, headers=SETUP)
        assert response.status_code == 200
        assert response.json() == {"quiet_level": level, "quiet_mode": level > 0}
        snapshot = client.get("/api/setup").json()
        assert snapshot["quiet_level"] == runtime.quiet_level == level
        assert snapshot["quiet_mode"] is (level > 0)
        assert snapshot["selected_model"] == "8b"
    assert settings.read() == {
        "selected_model": "8b",
        "quiet_level": level,
        "quiet_mode": level > 0,
    }


@pytest.mark.parametrize("level", [0, 7])
def test_saved_valid_level_has_priority_over_conflicting_legacy_bool(app_parts, level):
    runtime, settings, make = app_parts
    stored = {"quiet_level": level, "quiet_mode": level == 0}
    settings.update(**stored)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        snapshot = client.get("/api/setup").json()
        assert snapshot["quiet_level"] == runtime.quiet_level == level
        assert snapshot["quiet_mode"] is (level > 0)
    assert [value for value, _ in runtime.applied] == ([level] if level else [])
    assert settings.read() == stored  # Reading/migration never rewrites settings.


@pytest.mark.parametrize("legacy,level", [(True, 3), (False, 0)])
def test_legacy_settings_migrate_to_exact_default_levels(app_parts, legacy, level):
    runtime, settings, make = app_parts
    settings.update(quiet_mode=legacy)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert client.get("/api/setup").json()["quiet_level"] == runtime.quiet_level == level
    assert settings.read() == {"quiet_mode": legacy}


@pytest.mark.parametrize("invalid", [True, -1, 8, None, "1", 1.0])
def test_invalid_new_saved_levels_fall_back_to_valid_legacy_setting(app_parts, invalid):
    runtime, settings, make = app_parts
    settings.update(quiet_level=invalid, quiet_mode=True)
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        assert client.get("/api/setup").json()["quiet_level"] == runtime.quiet_level == 3


@pytest.mark.parametrize("value", [True, -1, 8, None, "1", 1.0])
def test_new_level_is_a_strict_bounded_integer_before_runtime_and_storage(app_parts, value):
    runtime, settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        result = client.post("/api/performance", json={"quiet_level": value}, headers=SETUP)
        assert result.status_code == 422
    assert not runtime.applied and not settings.path.exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"quiet_level": 3, "quiet_mode": True},
        {"quiet_level": 3, "unknown": True},
    ],
)
def test_only_one_recognized_performance_field_is_accepted(app_parts, body):
    runtime, settings, make = app_parts
    with TestClient(make(), base_url="http://127.0.0.1") as client:
        result = client.post("/api/performance", json=body, headers=SETUP)
        assert result.status_code == 422
    assert not runtime.applied and not settings.path.exists()


def test_persistence_failure_restores_a_previous_nonzero_level(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    store = SettingsStore(tmp_path / "settings.json")
    store.update(quiet_level=1, selected_model="8b")
    performance = PerformanceSettings(runtime, store)

    def unavailable(**changes):
        raise OSError("read-only settings")

    monkeypatch.setattr(store, "update", unavailable)
    with pytest.raises(RuntimeError, match="保存できません"):
        performance.set_quiet_level(3)
    assert performance.quiet_level == runtime.quiet_level == 1
    assert performance.snapshot() == {"quiet_level": 1, "quiet_mode": True}
    assert [level for level, _ in runtime.applied] == [1, 3, 1]
    assert store.read() == {"quiet_level": 1, "selected_model": "8b"}


def test_legacy_runtime_can_accept_compatibility_modes_but_not_other_presets(tmp_path):
    class LegacyRuntime:
        def __init__(self):
            self.applied = []

        def set_quiet_mode(self, enabled):
            self.applied.append(enabled)

    runtime = LegacyRuntime()
    store = SettingsStore(tmp_path / "settings.json")
    performance = PerformanceSettings(runtime, store)
    performance.set_quiet_mode(True)
    assert performance.quiet_level == 3 and runtime.applied == [True]
    with pytest.raises(RuntimeError, match="対応していません"):
        performance.set_quiet_level(1)
    assert performance.quiet_level == 3
    assert store.read() == {"quiet_level": 3, "quiet_mode": True}
    performance.set_quiet_mode(False)
    assert performance.quiet_level == 0 and runtime.applied == [True, False]

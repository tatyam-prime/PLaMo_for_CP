"""Parent IPC/lifecycle against a tiny in-process pipe peer, not a real DLL."""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from plamo_cp import native_engine as parent
from plamo_cp.engine import TranslationWork
from plamo_cp.native_ipc import read_frame, write_frame


class PipeChild:
    def __init__(self):
        child_in, parent_in = os.pipe()
        parent_out, child_out = os.pipe()
        self.stdin = os.fdopen(parent_in, "wb", buffering=0)
        self.stdout = os.fdopen(parent_out, "rb", buffering=0)
        self._incoming = os.fdopen(child_in, "rb", buffering=0)
        self._outgoing = os.fdopen(child_out, "wb", buffering=0)
        self.stderr = io.BytesIO()
        self.returncode = None
        self.terminated = False
        self.commands = []
        self.held = threading.Event()
        self.bad_id = False
        self.drop_quiet = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while frame := read_frame(self._incoming):
                self.commands.append(frame)
                kind = frame["type"]
                if kind == "init":
                    write_frame(
                        self._outgoing,
                        {"type": "ready", "version": 1, "info": {"device": "Fake GPU"}},
                    )
                elif kind == "submit":
                    if frame["text"].startswith("Hold"):
                        write_frame(
                            self._outgoing,
                            {"type": "preview", "id": frame["id"], "text": "途中訳。"},
                        )
                        self.held.set()
                    else:
                        write_frame(
                            self._outgoing,
                            {
                                "type": "result",
                                "id": "unknown" if self.bad_id else frame["id"],
                                "text": "訳文: " + frame["text"],
                            },
                        )
                elif kind == "cancel":
                    write_frame(self._outgoing, {"type": "cancelled", "id": frame["id"]})
                elif kind == "quiet" and not self.drop_quiet:
                    write_frame(
                        self._outgoing,
                        {"type": "quiet_applied", "id": frame["id"], "level": frame["level"]},
                    )
                elif kind == "close":
                    break
        except (OSError, ValueError):
            pass
        finally:
            self.returncode = 0
            self._incoming.close()
            self._outgoing.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired("fake native worker", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.stdin.close()

    kill = terminate


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    child = PipeChild()
    commands = []

    def popen(args, **options):
        commands.append((args, options))
        return child

    monkeypatch.setattr(parent.subprocess, "Popen", popen)
    runtime = SimpleNamespace(directory=tmp_path, bridge=tmp_path / "bridge.dll")
    native = parent.NativeEngine(runtime, cache_dir=tmp_path)
    monkeypatch.setattr(native, "model_available", lambda _: True)
    monkeypatch.setattr(native, "prepare", lambda *_: None)
    native._start_native(tmp_path / "weights.gguf", "2b", None)
    try:
        yield native, child, commands
    finally:
        native.close()
        assert child.poll() == 0 and not child._thread.is_alive()


def test_owned_child_uses_fixed_worker_command_and_does_not_auto_download(engine):
    native, child, commands = engine
    assert commands[0][0][-2:] == ["-m", "plamo_cp.native_worker"]
    assert commands[0][1]["stdin"] == subprocess.PIPE
    assert commands[0][1]["stderr"] == subprocess.PIPE
    assert child.commands[0]["type"] == "init" and child.commands[0]["quiet_level"] == 0
    assert native.status["native"] == {"device": "Fake GPU"}


def test_preview_live_quiet_and_individual_cancel_share_one_owned_worker(engine):
    native, child, _commands = engine
    previews, results = [], []
    held = TranslationWork("held", "Hold this paragraph.", {}, progress=previews.append)
    other = TranslationWork("other", "Find [[PLM_0]].", {})
    pending = [held, other]

    def run():
        results.extend(
            native.iter_work("2b", lambda _: [pending.pop(0)] if pending else [], lambda: 32)
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        assert child.held.wait(1)
        # The owner holds its lifecycle lock throughout; quiet must not need it.
        native.set_quiet_level(7)
        assert native.quiet_level == 7 and not child.terminated
        deadline = time.monotonic() + 1
        while not previews and time.monotonic() < deadline:
            time.sleep(0.005)
        assert previews == ["途中訳。"]
        held.cancelled.set()
        future.result(timeout=1)
    assert [(work.key, type(result).__name__) for work, result in results] == [
        ("other", "str"),
        ("held", "InterruptedError"),
    ]
    assert results[0][1] == "訳文: Find [[PLM_0]]."
    assert native._process is child and native._loaded == "2b" and not child.terminated
    assert native.status["state"] == "idle" and native._pending == {}


def test_bad_child_ids_fail_closed_without_corrupting_parent_state(engine):
    native, child, _commands = engine
    child.bad_id = True
    work = TranslationWork("a", "Find [[PLM_0]].", {})
    pending = [work]
    with pytest.raises(RuntimeError, match="通信"):
        list(native.iter_work("2b", lambda _: [pending.pop(0)] if pending else [], lambda: 32))
    assert native.status["state"] == "idle" and native._process is None
    assert native._pending == {} and native._loaded is None


def test_empty_iterator_does_not_send_model_work(engine):
    native, child, _commands = engine
    before = len(child.commands)
    assert list(native.iter_work("2b", lambda _: [], lambda: 32)) == []
    assert len(child.commands) == before


def test_crashed_or_closed_child_does_not_prevent_turning_quiet_off(engine):
    native, child, _commands = engine
    native.set_quiet_level(7)
    child.terminate()
    child.wait(1)
    native.set_quiet_level(0)
    assert native.quiet_level == 0


def test_bad_input_is_isolated_and_all_8000_api_characters_remain_supported(engine):
    native, child, _commands = engine
    works = [
        TranslationWork("nul", "Bad\x00paragraph", {}),
        TranslationWork("long", "x" * 8001, {}),
        TranslationWork("valid", "a" * 8000, {}),
    ]
    output = list(native.iter_work("2b", lambda _: [works.pop(0)] if works else [], lambda: 32))
    assert [type(result).__name__ for _work, result in output] == [
        "ValueError",
        "ValueError",
        "str",
    ]
    assert len([value for value in child.commands if value["type"] == "submit"]) == 1
    assert native._process is child and not child.terminated


def test_close_interrupts_the_owner_without_waiting_for_a_completed_translation(engine):
    native, child, _commands = engine
    work = TranslationWork("held", "Hold this paragraph.", {})
    works = [work]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            lambda: list(
                native.iter_work("2b", lambda _: [works.pop(0)] if works else [], lambda: 32)
            )
        )
        assert child.held.wait(1)
        native.close()
        with pytest.raises(InterruptedError):
            future.result(timeout=1)
    assert native._process is None and native.status["state"] == "idle"


@pytest.mark.parametrize("initial, requested", [(0, 7), (7, 0)])
def test_failed_native_load_uses_verified_cpu_backend_once_with_exact_quiet_intent(
    monkeypatch, tmp_path, initial, requested
):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    class CPU:
        def __init__(self, directory):
            assert directory == tmp_path
            self._lock = threading.RLock()
            self.status = {"backend": "llama.cpp", "loaded_model": None, "state": "idle"}
            self.level = 0

        def set_quiet_level(self, value):
            self.level = value

        def prepare(self, model, _progress=None):
            if self.status["loaded_model"] != model:
                entered.set()
                assert release.wait(2)
                calls.append(("cpu", model))
                self.status["loaded_model"] = model

        def _stop(self):
            self.status["loaded_model"] = None

        def close(self):
            self._stop()

    def fail(*_):
        calls.append(("gpu", "failed"))
        raise RuntimeError("driver crashed")

    monkeypatch.setattr(parent, "LlamaEngine", CPU)
    monkeypatch.setattr(parent.platform, "system", lambda: "Windows")
    monkeypatch.setattr(parent, "windows_memory_gb", lambda: 32)
    runtime = SimpleNamespace(directory=tmp_path, bridge=tmp_path / "bridge.dll")
    native = parent.NativeEngine(runtime, cache_dir=tmp_path)
    monkeypatch.setattr(native, "_verified_model", lambda *_: tmp_path / "model.gguf")
    monkeypatch.setattr(native, "_start_native", fail)
    native.set_quiet_level(initial)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(native.prepare, "2b")
            try:
                assert entered.wait(2)
                native.set_quiet_level(requested)
            finally:
                release.set()
            future.result(timeout=2)
        native.prepare("2b")
        assert calls == [("gpu", "failed"), ("cpu", "2b")]
        assert native._fallback.level == native.quiet_level == requested
        assert native.status["backend"] == "llama.cpp"
        assert "CPU" in native.status["native_fallback_reason"]
        native.set_quiet_level(3)
        assert native._fallback.level == 3 and native.quiet_level == 3
    finally:
        native.close()


def test_cancelled_load_never_starts_another_cpu_model(monkeypatch, tmp_path):
    monkeypatch.setattr(parent.platform, "system", lambda: "Windows")
    monkeypatch.setattr(parent, "windows_memory_gb", lambda: 32)
    native = parent.NativeEngine(
        SimpleNamespace(directory=tmp_path, bridge=tmp_path / "bridge.dll"), cache_dir=tmp_path
    )
    monkeypatch.setattr(native, "_verified_model", lambda *_: tmp_path / "model.gguf")

    def cancel(*_):
        raise InterruptedError

    monkeypatch.setattr(native, "_start_native", cancel)
    try:
        with pytest.raises(InterruptedError):
            native.prepare("2b")
        assert native._fallback is None and native.status["state"] == "idle"
    finally:
        native.close()


def test_fallback_unload_uses_actual_loaded_model_after_an_internal_switch(engine):
    native, _child, _commands = engine
    stopped = []
    native._stop()
    native._loaded = "2b"  # Stale parent snapshot from before a fallback cohort.
    native._fallback = SimpleNamespace(
        status={"backend": "llama.cpp", "loaded_model": "8b", "state": "idle"},
        _lock=threading.RLock(),
        _stop=lambda: stopped.append("8b"),
    )
    native.unload("2b")
    assert stopped == []
    native.unload("8b")
    assert stopped == ["8b"] and native._loaded is None
    native._fallback = None

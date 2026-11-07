"""Quiet pacing without Metal initialization, weights, or wall-clock benchmarks."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from plamo_cp import engine as runtime
from plamo_cp.performance import LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL


def fake_mlx(monkeypatch, synchronize):
    parent, core = ModuleType("mlx"), ModuleType("mlx.core")
    core.synchronize = synchronize
    parent.core = core
    monkeypatch.setitem(sys.modules, "mlx", parent)
    monkeypatch.setitem(sys.modules, "mlx.core", core)


def test_default_off_never_synchronizes_or_waits(monkeypatch, tmp_path):
    engine = runtime.Engine(cache_dir=tmp_path)
    assert engine.quiet_mode is False
    fake_mlx(monkeypatch, lambda *_: pytest.fail("normal mode must not synchronize"))
    monkeypatch.setattr(engine, "_quiet_wait", lambda *_: pytest.fail("must not wait"))
    engine._pace_quiet_step(None, threading.Event(), object())
    engine._pace_quiet_step(1, threading.Event(), object())


@pytest.mark.parametrize("invalid", [0, 1, "true", None, []])
def test_runtime_quiet_setting_is_strictly_boolean(tmp_path, invalid):
    engine = runtime.Engine(cache_dir=tmp_path)
    with pytest.raises(ValueError):
        engine.set_quiet_mode(invalid)
    assert engine.quiet_mode is False


def test_adaptive_measurement_includes_completion_on_the_exact_gpu_stream(monkeypatch, tmp_path):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_mode(True)
    clock = SimpleNamespace(now=10.02)
    expected_stream, synchronized, waits = object(), [], []

    def synchronize(stream):
        synchronized.append(stream)
        clock.now += 0.04  # Work was queued asynchronously before Python yielded.

    fake_mlx(monkeypatch, synchronize)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(engine, "_quiet_wait", lambda delay, *_: waits.append(delay))
    engine._pace_quiet_step(10.0, threading.Event(), expected_stream)
    assert synchronized == [expected_stream]
    assert waits == [pytest.approx(0.06)]


@pytest.mark.parametrize(
    "level,pause",
    [
        (level, 0.25 * (5 ** (level / MAX_QUIET_LEVEL) - 1))
        for level in range(1, MAX_QUIET_LEVEL + 1)
    ],
)
def test_long_compute_step_keeps_distinct_proportional_pause_caps(
    monkeypatch, tmp_path, level, pause
):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_level(level)
    clock, waits = SimpleNamespace(now=0.0), []

    class Condition:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            clock.now += seconds

    engine._quiet_condition = Condition()
    monkeypatch.setattr(runtime, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    engine._quiet_wait(10, threading.Event())
    assert sum(waits) == pytest.approx(pause)
    assert all(0 < wait <= 0.02 for wait in waits)


@pytest.mark.parametrize("action", ["cancel", "disable", "all-pages-cancelled"])
@pytest.mark.parametrize("level", [LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL])
def test_quiet_pause_wakes_for_cancellation_or_a_live_disable(tmp_path, monkeypatch, action, level):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_level(level)
    cancelled, entered, active = threading.Event(), threading.Event(), threading.Event()
    active.set()
    original_wait = engine._quiet_condition.wait

    def wait(seconds):
        entered.set()
        return original_wait(seconds)

    monkeypatch.setattr(engine._quiet_condition, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(engine._quiet_wait, 0.25, cancelled, active.is_set)
        assert entered.wait(timeout=1)
        if action == "cancel":
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=0.2)
        else:
            if action == "disable":
                engine.set_quiet_mode(False)
            else:
                active.clear()
            assert future.result(timeout=0.2) is None


def test_toggling_does_not_wait_for_the_inference_lock_or_load_a_model(tmp_path, monkeypatch):
    engine = runtime.Engine(cache_dir=tmp_path)
    monkeypatch.setattr(engine, "_load", lambda *_: pytest.fail("must not load a model"))
    engine._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(engine.set_quiet_mode, True).result(timeout=0.5)
        assert engine.quiet_mode is True and engine.status["loaded_model"] is None
    finally:
        engine._lock.release()


def test_cancelled_or_empty_active_pool_does_not_synchronize(monkeypatch, tmp_path):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_mode(True)
    fake_mlx(monkeypatch, lambda *_: pytest.fail("cancelled work must not synchronize"))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        engine._pace_quiet_step(0, cancelled, object())
    engine._pace_quiet_step(0, threading.Event(), object(), active=lambda: False)


@pytest.mark.parametrize("invalid", [-1, MAX_QUIET_LEVEL + 1, 1.5, True, False, "2", None])
def test_quiet_level_is_a_strict_bounded_integer(tmp_path, invalid):
    engine = runtime.Engine(cache_dir=tmp_path)
    with pytest.raises(ValueError):
        engine.set_quiet_level(invalid)
    assert engine.quiet_level == 0 and engine.quiet_mode is False


def test_boolean_compatibility_uses_nearest_half_duty_level_and_off(tmp_path):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_level(3)
    assert engine.quiet_level == 3 and engine.quiet_mode is True
    engine.set_quiet_mode(True)
    assert engine.quiet_level == LEGACY_QUIET_LEVEL
    engine.set_quiet_mode(False)
    assert engine.quiet_level == 0 and engine.quiet_mode is False


def test_weakening_a_live_pause_shortens_its_existing_target(tmp_path, monkeypatch):
    engine = runtime.Engine(cache_dir=tmp_path)
    engine.set_quiet_level(MAX_QUIET_LEVEL)
    entered = threading.Event()
    original_wait = engine._quiet_condition.wait

    def wait(seconds):
        entered.set()
        return original_wait(seconds)

    monkeypatch.setattr(engine._quiet_condition, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(engine._quiet_wait, 0.25, threading.Event())
        assert entered.wait(timeout=1)
        engine.set_quiet_level(LEGACY_QUIET_LEVEL)
        assert future.result(timeout=0.6) is None  # Level 7 would wait 1s.
    assert engine.quiet_level == LEGACY_QUIET_LEVEL

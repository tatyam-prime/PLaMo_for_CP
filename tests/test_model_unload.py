from __future__ import annotations

import sys
from types import ModuleType

from plamo_cp.engine import Engine
from plamo_cp.llama_backend import LlamaEngine


def test_never_loaded_mlx_unload_does_not_initialize_metal(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx", None)
    engine = Engine()
    engine.unload()
    assert engine._model is None
    assert engine._loaded is None


def test_unloading_another_size_preserves_active_mlx_weights(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx", None)
    engine = Engine()
    model = object()
    engine._model, engine._loaded = model, "2b"
    engine.unload("0.8b")
    assert engine._model is model
    assert engine._loaded == "2b"


def test_active_mlx_unload_releases_weights_but_keeps_verified_tokenizer(monkeypatch):
    mlx, core = ModuleType("mlx"), ModuleType("mlx.core")
    calls = []
    core.clear_cache = lambda: calls.append("clear")
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    engine = Engine()
    tokenizer = object()
    engine._model, engine._tokenizer, engine._loaded = object(), tokenizer, "2b"
    engine._tokenizer_cache["verified"] = tokenizer
    engine.unload("2b")
    assert engine._model is None and engine._tokenizer is None
    assert engine._loaded is None
    assert engine._state == "idle"
    assert calls == ["clear"]
    assert engine._tokenizer_cache == {"verified": tokenizer}


def test_deleting_another_size_does_not_stop_owned_llama_process(monkeypatch):
    engine = LlamaEngine()
    engine._loaded = "2b"
    calls = []
    monkeypatch.setattr(engine, "_stop", lambda: calls.append("stop"))
    engine.unload("0.8b")
    assert calls == []
    assert engine._loaded == "2b"


def test_active_llama_unload_stops_only_owned_process(monkeypatch):
    engine = LlamaEngine()
    engine._loaded = "2b"
    calls = []

    def stop():
        calls.append("stop")
        engine._loaded = None

    monkeypatch.setattr(engine, "_stop", stop)
    engine.unload("2b")
    assert calls == ["stop"]
    assert engine._loaded is None
    assert engine._state == "idle"

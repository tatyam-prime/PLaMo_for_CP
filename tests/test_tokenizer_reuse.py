from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from plamo_cp import engine as module

RESOURCES = (
    "tokenizer.jsonl",
    "tokenization_plamo.py",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "chat_template.jinja",
    "generation_config.json",
)


def checkpoint(path: Path) -> Path:
    path.mkdir()
    for name in RESOURCES:
        (path / name).write_text(name, encoding="utf-8")
    (path / "config.json").write_text(
        json.dumps(
            {
                "tokenizer_class": "Plamo3Tokenizer",
                "bos_token_id": 1,
                "eos_token_id": 1,
                "pad_token_id": 3,
                "vocab_size": 107520,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def loaders(monkeypatch, tmp_path):
    paths = {size: checkpoint(tmp_path / size) for size in ("0.8b", "2b")}
    full_calls, weight_calls = [], []

    class Tokenizer:
        def __init__(self):
            self.eos = set()

        def add_eos_token(self, value):
            self.eos.add(value)

    def full_load(path, **kwargs):
        full_calls.append((path, kwargs))
        return object(), Tokenizer()

    def weights_load(path, **kwargs):
        weight_calls.append((path, kwargs))
        return object(), {}

    mlx = ModuleType("mlx")
    core = ModuleType("mlx.core")
    core.clear_cache = lambda: None
    mlx.core = core
    mlx_lm = ModuleType("mlx_lm")
    mlx_lm.load = full_load
    utils = ModuleType("mlx_lm.utils")
    utils.load_model = weights_load
    for name, value in {
        "mlx": mlx,
        "mlx.core": core,
        "mlx_lm": mlx_lm,
        "mlx_lm.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *_: str(24 * 1024**3).encode())
    by_revision = {module.MODEL_SPECS[size]["revision"]: path for size, path in paths.items()}
    monkeypatch.setattr(
        "plamo_cp.download.cached_snapshot", lambda spec, **_: str(by_revision[spec["revision"]])
    )
    monkeypatch.setattr(
        module, "download_model", lambda *_, **__: pytest.fail("cached loading cannot download")
    )
    return SimpleNamespace(
        engine=module.Engine(), paths=paths, full=full_calls, weights=weight_calls
    )


def test_identical_tokenizer_is_not_rebuilt_across_model_switches(loaders):
    engine = loaders.engine
    engine.prepare("0.8b")
    tokenizer = engine._tokenizer
    first_model = engine._model
    engine.prepare("2b")
    assert engine._tokenizer is tokenizer
    assert engine._model is not first_model
    engine.prepare("0.8b")
    assert engine._tokenizer is tokenizer
    assert len(loaders.full) == 1
    assert len(loaders.weights) == 2
    assert all(kwargs == {"trust_remote_code": True} for _, kwargs in loaders.weights)
    assert tokenizer.eos == {"<|plamo:tag|>", "<|plamo:bos|>"}


def test_already_loaded_model_does_no_work(loaders):
    loaders.engine.prepare("2b")
    loaders.engine.prepare("2b")
    assert len(loaders.full) == 1
    assert loaders.weights == []


def test_cached_tokenizer_cannot_fetch_remote_code_or_missing_files(loaders):
    loaders.engine.prepare("2b")
    assert loaders.full[0][1]["tokenizer_config"] == {
        "trust_remote_code": True,
        "local_files_only": True,
    }


@pytest.mark.parametrize("resource", RESOURCES)
def test_any_changed_tokenizer_resource_prevents_reuse(loaders, resource):
    loaders.engine.prepare("0.8b")
    first = loaders.engine._tokenizer
    (loaders.paths["2b"] / resource).write_text("different", encoding="utf-8")
    loaders.engine.prepare("2b")
    assert loaders.engine._tokenizer is not first
    assert len(loaders.full) == 2
    assert loaders.weights == []


def test_changed_config_token_binding_prevents_reuse(loaders):
    loaders.engine.prepare("0.8b")
    config = loaders.paths["2b"] / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["eos_token_id"] = 99
    config.write_text(json.dumps(data), encoding="utf-8")
    loaders.engine.prepare("2b")
    assert len(loaders.full) == 2


def test_model_architecture_changes_do_not_rebuild_identical_tokenizer(loaders):
    loaders.engine.prepare("0.8b")
    config = loaders.paths["2b"] / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["hidden_size"] = 2048
    config.write_text(json.dumps(data), encoding="utf-8")
    loaders.engine.prepare("2b")
    assert len(loaders.full) == 1
    assert len(loaders.weights) == 1


def test_missing_resource_disables_reuse(tmp_path):
    path = checkpoint(tmp_path / "checkpoint")
    (path / "chat_template.jinja").unlink()
    assert module.tokenizer_fingerprint(path) is None


def test_weight_load_failure_keeps_no_wrong_model_and_retains_safe_tokenizer(loaders, monkeypatch):
    loaders.engine.prepare("0.8b")

    def fail(*_args, **_kwargs):
        raise RuntimeError("weights failed")

    monkeypatch.setattr(sys.modules["mlx_lm.utils"], "load_model", fail)
    with pytest.raises(RuntimeError, match="weights failed"):
        loaders.engine.prepare("2b")
    assert loaders.engine._model is None
    assert loaders.engine._loaded is None
    assert loaders.engine._state == "idle"
    assert len(loaders.engine._tokenizer_cache) == 1


def test_cached_model_switch_never_enters_the_download_writer_lock(loaders, monkeypatch):
    monkeypatch.setattr(
        "plamo_cp.download._download_lock",
        lambda *_: pytest.fail("cached model cannot wait for another download"),
    )
    loaders.engine.prepare("0.8b")
    loaders.engine.prepare("2b")
    assert loaders.engine._loaded == "2b" and len(loaders.weights) == 1


def test_missing_cached_switch_keeps_the_current_live_model_and_never_downloads(
    loaders, monkeypatch
):
    loaders.engine.prepare("0.8b")
    current = loaders.engine._model
    monkeypatch.setattr("plamo_cp.download.cached_snapshot", lambda *_, **__: None)
    with pytest.raises(RuntimeError, match="ダウンロード"):
        loaders.engine.prepare("2b")
    assert loaders.engine._loaded == "0.8b" and loaders.engine._model is current

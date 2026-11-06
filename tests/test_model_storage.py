"""Only temporary, tiny HF-like fixtures are removed; never the real model cache."""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from plamo_cp import model_storage as storage_module
from plamo_cp.model_storage import ModelStorage


class Engine:
    def __init__(self):
        self.model_specs = {
            "2b": {
                "name": "PLaMo 2B",
                "repo": "pfnet/plamo-3-translate-2b-GGUF",
                "revision": "a" * 40,
                "filename": "weights.gguf",
                "weight_bytes": 3,
                "weight_gb": 1.81,
            },
            "8b": {
                "name": "PLaMo 8B",
                "repo": "pfnet/plamo-3-translate-8b-GGUF",
                "revision": "b" * 40,
                "filename": "weights.gguf",
                "weight_bytes": 4,
                "weight_gb": 5.28,
            },
        }
        self.status = {"loaded_model": "2b"}
        self.events = []

    def unload(self, model):
        self.events.append(("unload", model))
        if self.status["loaded_model"] == model:
            self.status["loaded_model"] = None


def repo_path(cache, repo_id):
    return cache / ("models--" + repo_id.replace("/", "--"))


def make_snapshot(cache, spec, *, revision=None, content=b"123"):
    repo = repo_path(cache, spec["repo"])
    snapshot = repo / "snapshots" / (revision or spec["revision"])
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / spec["filename"]).write_bytes(content)
    return repo, snapshot


class Strategy:
    def __init__(self, repo, cache, engine):
        self.repos, self.snapshots, self.refs = frozenset({repo}), frozenset(), frozenset()
        self.files, self.blobs = frozenset(), frozenset()
        self.cache_dir, self.engine, self.executed = cache, engine, False

    def execute(self):
        assert self.engine.events == [("unload", "2b")]
        self.executed = True
        for repo in self.repos:
            shutil.rmtree(repo)


def fake_scan(monkeypatch, cache, repo, engine, revisions, *, strategy=None):
    strategy = strategy or Strategy(repo, cache, engine)
    calls = []
    selected = SimpleNamespace(
        repo_type="model",
        repo_id=engine.model_specs["2b"]["repo"],
        repo_path=repo,
        revisions=[SimpleNamespace(commit_hash=value) for value in revisions],
    )

    def delete_revisions(*values):
        calls.append(values)
        return strategy

    info = SimpleNamespace(repos=[selected], delete_revisions=delete_revisions)
    monkeypatch.setattr(storage_module, "scan_cache_dir", lambda folder: info)
    return strategy, calls


def test_snapshot_uses_the_actual_folder_and_stats_only_known_repos(tmp_path, monkeypatch):
    engine = Engine()
    cache = tmp_path / "hub"
    repo, snapshot = make_snapshot(cache, engine.model_specs["2b"])
    other, _ = make_snapshot(cache, engine.model_specs["8b"], content=b"1234")
    (other / "blobs").mkdir()
    (other / "blobs/partial.incomplete").write_bytes(b"partial")
    monkeypatch.setattr(
        storage_module,
        "scan_cache_dir",
        lambda *_: pytest.fail("readonly UI must not scan the full cache"),
    )
    state = ModelStorage(engine, cache).snapshot()
    assert state["folder"] == str(cache.resolve()) and state["shared"] is False
    first, second = state["models"]
    assert first == {
        "id": "2b",
        "name": "PLaMo 2B",
        "weight_gb": 1.81,
        "downloaded": True,
        "path": str(snapshot / "weights.gguf"),
        "on_disk_bytes": 3,
        "loaded": True,
    }
    assert second["on_disk_bytes"] == 11
    assert second["loaded"] is False
    assert repo.is_dir()


def test_missing_models_have_null_paths_and_do_not_create_a_cache_folder(tmp_path):
    cache = tmp_path / "does-not-exist"
    state = ModelStorage(Engine(), cache).snapshot()
    assert all(not item["downloaded"] and item["path"] is None for item in state["models"])
    assert not cache.exists()


def test_delete_targets_all_revisions_and_unloads_before_executing_plan(tmp_path, monkeypatch):
    engine, cache = Engine(), tmp_path / "hub"
    repo, _ = make_snapshot(cache, engine.model_specs["2b"])
    make_snapshot(cache, engine.model_specs["2b"], revision="c" * 40, content=b"older")
    other, _ = make_snapshot(cache, engine.model_specs["8b"], content=b"keep")
    strategy, calls = fake_scan(monkeypatch, cache, repo, engine, ["a" * 40, "c" * 40])
    result = ModelStorage(engine, cache).delete("2b")
    assert result == {"deleted": True, "freed_bytes": 8}
    assert strategy.executed
    assert set(calls[0]) == {"a" * 40, "c" * 40}
    assert other.is_dir()


def test_partial_only_download_can_be_deleted_without_sweeping_global_blobs(tmp_path, monkeypatch):
    engine, cache = Engine(), tmp_path / "hub"
    repo = repo_path(cache, engine.model_specs["2b"]["repo"])
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs/weight.incomplete").write_bytes(b"partial")
    global_blob = cache / "blobs/aa/payload"
    global_blob.parent.mkdir(parents=True)
    global_blob.write_bytes(b"must stay")
    monkeypatch.setattr(storage_module, "scan_cache_dir", lambda *_: SimpleNamespace(repos=[]))
    manager = ModelStorage(engine, cache)
    item = manager.snapshot()["models"][0]
    assert item["downloaded"] is False and item["on_disk_bytes"] == 7
    assert manager.delete("2b") == {"deleted": True, "freed_bytes": 7}
    assert global_blob.read_bytes() == b"must stay"


@pytest.mark.parametrize("field", ["repos", "snapshots", "refs", "files", "blobs", "cache_dir"])
def test_plan_cannot_delete_another_repo_or_outside_cache(tmp_path, monkeypatch, field):
    engine, cache = Engine(), tmp_path / "hub"
    repo, _ = make_snapshot(cache, engine.model_specs["2b"])
    other, _ = make_snapshot(cache, engine.model_specs["8b"], content=b"keep")
    strategy = Strategy(repo, cache, engine)
    path = other if field != "blobs" else tmp_path / "outside-blob"
    setattr(strategy, field, path if field == "cache_dir" else frozenset({path}))
    fake_scan(monkeypatch, cache, repo, engine, ["a" * 40], strategy=strategy)
    with pytest.raises(ValueError):
        ModelStorage(engine, cache).delete("2b")
    assert not strategy.executed
    assert repo.is_dir() and other.is_dir()


def test_unknown_model_is_rejected_before_any_filesystem_action(tmp_path, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(storage_module, "scan_cache_dir", lambda *_: pytest.fail("unknown model"))
    with pytest.raises(ValueError):
        ModelStorage(engine, tmp_path / "hub").delete("../../other")
    assert engine.events == []


@pytest.mark.skipif(
    os.name == "nt", reason="Unix symlink fixture; Windows copied snapshots are covered separately"
)
def test_shared_payload_is_counted_once_and_not_reported_freed_while_another_repo_uses_it(
    tmp_path, monkeypatch
):
    engine, cache = Engine(), tmp_path / "hub"
    shared = cache / "blobs/aa/shared"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"123")
    repo = repo_path(cache, engine.model_specs["2b"]["repo"])
    snapshot = repo / "snapshots" / engine.model_specs["2b"]["revision"]
    snapshot.mkdir(parents=True)
    (repo / "blobs").mkdir()
    (repo / "blobs/payload").symlink_to(shared)
    (snapshot / "weights.gguf").symlink_to(repo / "blobs/payload")
    other = repo_path(cache, engine.model_specs["8b"]["repo"])
    other.mkdir()
    (other / "shared-reference").symlink_to(shared)
    fake_scan(monkeypatch, cache, repo, engine, ["a" * 40])
    manager = ModelStorage(engine, cache)
    assert manager.snapshot()["models"][0]["on_disk_bytes"] == 3
    assert manager.delete("2b") == {"deleted": True, "freed_bytes": 0}
    assert shared.read_bytes() == b"123" and (other / "shared-reference").is_file()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink safety fixture")
def test_external_symlink_is_never_followed_or_deleted(tmp_path):
    engine, cache = Engine(), tmp_path / "hub"
    outside = tmp_path / "outside"
    outside.write_bytes(b"private data")
    repo = repo_path(cache, engine.model_specs["2b"]["repo"])
    repo.mkdir(parents=True)
    (repo / "external").symlink_to(outside)
    manager = ModelStorage(engine, cache)
    assert manager.snapshot()["models"][0]["on_disk_bytes"] == 0
    with pytest.raises(ValueError):
        manager.delete("2b")
    assert outside.read_bytes() == b"private data" and repo.is_dir()


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_open_folder_opens_only_the_displayed_fixed_directory(tmp_path, monkeypatch, system):
    cache, opened = tmp_path / "model folder", []
    monkeypatch.setattr(storage_module.platform, "system", lambda: system)
    monkeypatch.setattr(
        storage_module.subprocess, "Popen", lambda arguments: opened.append(arguments)
    )
    monkeypatch.setattr(
        storage_module.os, "startfile", lambda path: opened.append(path), raising=False
    )
    manager = ModelStorage(Engine(), cache)
    manager.open_folder()
    assert cache.is_dir()
    assert opened == (
        [["open", str(cache.resolve())]] if system == "Darwin" else [str(cache.resolve())]
    )


def test_missing_model_delete_is_idempotent(tmp_path):
    engine = Engine()
    assert ModelStorage(engine, tmp_path / "hub").delete("2b") == {
        "deleted": False,
        "freed_bytes": 0,
    }
    assert engine.events == [("unload", "2b")]


@pytest.mark.skipif(
    os.name == "nt", reason="Actual shared-store integration needs Unix symlink privileges"
)
def test_genuine_hf_shared_store_strategy_keeps_other_repo_then_frees_last_reference(tmp_path):
    from huggingface_hub.utils import _shared_blobs

    engine, cache = Engine(), tmp_path / "hub"
    cache.mkdir()
    xet_hash = "d" * 64
    for model in ("2b", "8b"):
        spec = engine.model_specs[model]
        repo = repo_path(cache, spec["repo"])
        blobs = repo / "blobs"
        blobs.mkdir(parents=True)
        blob = blobs / "etag"
        if model == "2b":
            blob.write_bytes(b"123")
            assert _shared_blobs.publish_blob_to_shared_store(
                blob_path=str(blob),
                xet_hash=xet_hash,
                cache_dir=cache,
                expected_size=3,
            )
        else:
            assert _shared_blobs.try_link_from_shared_store(
                blob_path=str(blob),
                xet_hash=xet_hash,
                cache_dir=cache,
                expected_size=3,
            )
        snapshot = repo / "snapshots" / spec["revision"]
        snapshot.mkdir(parents=True)
        (snapshot / "weights.gguf").symlink_to(blob)
    shared = _shared_blobs.shared_blob_path(cache, xet_hash)
    assert shared.read_bytes() == b"123"
    # This is the real installed scanner/deletion strategy, against a tiny temp
    # directory, not a mock and never the user's real HF cache.
    manager = ModelStorage(engine, cache)
    assert manager.delete("2b") == {"deleted": True, "freed_bytes": 0}
    assert shared.read_bytes() == b"123"
    assert (repo_path(cache, engine.model_specs["8b"]["repo"]) / "blobs/etag").is_file()
    assert manager.delete("8b") == {"deleted": True, "freed_bytes": 3}
    assert not shared.exists()


def test_snapshot_tolerates_an_atomic_download_file_replacement(tmp_path, monkeypatch):
    engine, cache = Engine(), tmp_path / "hub"
    make_snapshot(cache, engine.model_specs["2b"])
    manager = ModelStorage(engine, cache)
    monkeypatch.setattr(
        manager, "_available_path_checked", lambda *_: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert all(not model["downloaded"] for model in manager.snapshot()["models"])


def test_folder_change_persists_without_moving_old_models_or_losing_selected_model(tmp_path):
    engine = Engine()
    old, new = tmp_path / "old", tmp_path / "新しいモデル保存先"
    old_repo, old_snapshot = make_snapshot(old, engine.model_specs["2b"])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "8b", "other": True}), encoding="utf-8")
    manager = ModelStorage(engine, old, settings_path=settings)
    state = manager.set_folder(new)
    assert state["folder"] == str(new.resolve()) and state["shared"] is False
    assert all(not item["downloaded"] for item in state["models"])
    assert engine.cache_dir == new.resolve()
    assert (old_snapshot / "weights.gguf").read_bytes() == b"123" and old_repo.is_dir()
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "selected_model": "8b",
        "model_folder": str(new.resolve()),
        "other": True,
    }
    restarted = ModelStorage(Engine(), settings_path=settings)
    assert restarted.snapshot()["folder"] == str(new.resolve())
    assert restarted.engine.cache_dir == new.resolve()
    restarted.set_folder(old)
    assert restarted.snapshot()["models"][0]["downloaded"] is True


def test_delete_and_open_follow_new_destination_and_leave_previous_repo(tmp_path, monkeypatch):
    engine, old, new = Engine(), tmp_path / "old", tmp_path / "new"
    old_repo, _ = make_snapshot(old, engine.model_specs["2b"])
    new_repo, _ = make_snapshot(new, engine.model_specs["2b"])
    manager = ModelStorage(engine, old, settings_path=tmp_path / "settings.json")
    manager.set_folder(new)
    engine.events.clear()
    monkeypatch.setattr(storage_module, "scan_cache_dir", lambda *_: SimpleNamespace(repos=[]))
    assert manager.delete("2b") == {"deleted": True, "freed_bytes": 3}
    assert old_repo.is_dir() and not new_repo.exists()
    opened = []
    monkeypatch.setattr(storage_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(storage_module.subprocess, "Popen", lambda args: opened.append(args))
    manager.open_folder()
    assert opened == [["open", str(new.resolve())]]


def test_picker_cancellation_does_not_save_or_create_a_new_folder(tmp_path, monkeypatch):
    from plamo_cp import folder_picker

    old, settings = tmp_path / "old", tmp_path / "settings.json"
    manager = ModelStorage(Engine(), old, settings_path=settings)
    selected = []
    monkeypatch.setattr(folder_picker, "choose_folder", lambda initial: selected.append(initial))
    assert manager.choose_folder() == {"cancelled": True}
    assert selected == [old.resolve()] and not settings.exists() and not old.exists()


def test_picker_selection_changes_the_actual_storage_folder(tmp_path, monkeypatch):
    from plamo_cp import folder_picker

    new = tmp_path / "選択した保存先"
    monkeypatch.setattr(folder_picker, "choose_folder", lambda initial: new)
    manager = ModelStorage(Engine(), tmp_path / "old", settings_path=tmp_path / "settings.json")
    assert manager.choose_folder()["folder"] == str(new.resolve())
    assert new.is_dir()


@pytest.mark.parametrize("folder", ["", "relative-folder", "\x00bad"])
def test_invalid_folder_is_rejected_without_changing_settings(tmp_path, folder):
    settings = tmp_path / "settings.json"
    manager = ModelStorage(Engine(), tmp_path / "old", settings_path=settings)
    with pytest.raises(ValueError):
        manager.set_folder(folder)
    assert manager.cache_dir == (tmp_path / "old").resolve() and not settings.exists()


def test_unwritable_folder_is_rejected_before_unloading_or_saving(tmp_path, monkeypatch):
    engine, old = Engine(), tmp_path / "old"
    manager = ModelStorage(engine, old, settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(
        storage_module.tempfile,
        "TemporaryFile",
        lambda **_: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(ValueError, match="保存できません"):
        manager.set_folder(tmp_path / "new")
    assert manager.cache_dir == old.resolve() and engine.events == []


def test_failed_settings_write_keeps_the_original_destination(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    manager = ModelStorage(Engine(), old, settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(
        manager.settings, "update", lambda **_: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        manager.set_folder(new)
    assert manager.cache_dir == old.resolve() and manager.engine.cache_dir == old.resolve()


def test_only_the_hf_default_directory_is_reported_as_shared(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(storage_module.constants, "HF_HUB_CACHE", str(shared))
    assert ModelStorage(Engine(), shared).snapshot()["shared"] is True
    assert ModelStorage(Engine(), tmp_path / "custom").snapshot()["shared"] is False

from __future__ import annotations

import ast
import copy
import ctypes
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from plamo_cp import native_runtime, runtime
from scripts import prepare_native_runtime, smoke_desktop_bundle, smoke_native_runtime


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path.resolve() / "native"
    directory.mkdir()
    for name in (
        "cp_llama_bridge.dll",
        "llama.dll",
        "ggml.dll",
        "ggml-base.dll",
        "ggml-cpu-haswell.dll",
        "ggml-vulkan.dll",
        "LICENSE",
    ):
        (directory / name).write_bytes(name.encode())
    pins = prepare_native_runtime.create_pins(directory, "x64")
    return directory, pins


def test_fixed_inventory_accepts_only_exact_files_and_no_sidecar_can_override(bundle):
    directory, pins = bundle
    spec = native_runtime.verify_native_runtime_directory(directory, pins)
    assert spec.directory == directory
    assert spec.bridge == directory / "cp_llama_bridge.dll"
    assert spec.architecture == "x64"
    (directory / "untrusted-manifest.json").write_text(json.dumps(pins))
    with pytest.raises(RuntimeError, match="未登録"):
        native_runtime.verify_native_runtime_directory(directory, pins)


@pytest.mark.parametrize("mutation", ["change", "same-size", "missing", "extra-dll"])
def test_missing_changed_and_unknown_native_code_is_rejected(bundle, mutation):
    directory, pins = bundle
    path = directory / "llama.dll"
    if mutation == "missing":
        path.unlink()
    elif mutation == "extra-dll":
        (directory / "ggml-untrusted.dll").write_bytes(b"unexpected")
    elif mutation == "same-size":
        path.write_bytes(b"x" * path.stat().st_size)
    else:
        path.write_bytes(b"changed")
    with pytest.raises(RuntimeError):
        native_runtime.verify_native_runtime_directory(directory, pins)


@pytest.mark.parametrize(
    "name",
    [
        "../escape.dll",
        "/absolute.dll",
        "a//b",
        "a/./b",
        "a\\b",
        "C:evil.dll",
        "a:stream",
        "NUL.dll",
        "COM1",
        "nested/LPT².txt",
        "trailing. ",
        "a\x00b",
    ],
)
def test_windows_aliases_streams_and_traversal_are_not_pin_names(bundle, name):
    directory, pins = bundle
    pins["files"][name] = {"sha256": "0" * 64, "size": 0}
    with pytest.raises(RuntimeError):
        native_runtime.verify_native_runtime_directory(directory, pins)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("bridge_abi", True),
        ("bridge_abi", 2),
        ("llama_release", "latest"),
        ("architecture", "other"),
    ],
)
def test_pins_schema_release_and_abi_are_strict(bundle, field, value):
    directory, pins = bundle
    pins[field] = value
    with pytest.raises(RuntimeError):
        native_runtime.verify_native_runtime_directory(directory, pins)


def test_nested_dll_and_case_collisions_are_rejected(bundle):
    directory, pins = bundle
    pins["files"]["plugins/evil.dll"] = {"sha256": "0" * 64, "size": 0}
    with pytest.raises(RuntimeError):
        native_runtime.verify_native_runtime_directory(directory, pins)
    del pins["files"]["plugins/evil.dll"]
    pins["files"]["LLAMA.dll"] = pins["files"]["llama.dll"]
    with pytest.raises(RuntimeError, match="重複"):
        native_runtime.verify_native_runtime_directory(directory, pins)


@pytest.mark.parametrize("link_kind", ["file", "directory", "root", "ancestor"])
def test_native_paths_never_follow_symbolic_links(bundle, tmp_path, link_kind):
    directory, pins = bundle
    try:
        if link_kind == "file":
            path = directory / "llama.dll"
            target = tmp_path / "outside.dll"
            path.replace(target)
            path.symlink_to(target)
        elif link_kind == "directory":
            (directory / "linked").symlink_to(tmp_path, target_is_directory=True)
        elif link_kind == "root":
            link = tmp_path / "linked-native"
            link.symlink_to(directory, target_is_directory=True)
            directory = link
        else:
            link = tmp_path / "linked-parent"
            link.symlink_to(tmp_path, target_is_directory=True)
            directory = link / "native"
    except OSError:
        pytest.skip("Creating symlinks requires developer permissions on this Windows runner")
    with pytest.raises(RuntimeError, match="リンク"):
        native_runtime.verify_native_runtime_directory(directory, pins)


def test_windows_reparse_point_is_rejected_without_recursing(bundle, monkeypatch):
    directory, pins = bundle
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == directory:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(RuntimeError, match="リンク"):
        native_runtime.verify_native_runtime_directory(directory, pins)


def test_source_checkout_uses_cpu_and_frozen_uses_fixed_meipass(bundle, monkeypatch):
    directory, pins = bundle
    monkeypatch.setattr(native_runtime._native_runtime_pins, "RUNTIME_PINS", None)
    assert native_runtime.verified_native_runtime() is None
    monkeypatch.setattr(native_runtime._native_runtime_pins, "RUNTIME_PINS", pins)
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(RuntimeError, match="配布アプリ"):
        native_runtime.verified_native_runtime()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(directory.parent), raising=False)
    from plamo_cp import llama_backend

    monkeypatch.setattr(llama_backend, "runtime_arch", lambda: "x64")
    assert native_runtime.verified_native_runtime().bridge == directory / "cp_llama_bridge.dll"
    monkeypatch.setattr(llama_backend, "runtime_arch", lambda: "arm64")
    with pytest.raises(RuntimeError, match="種類"):
        native_runtime.verified_native_runtime()


def test_factory_selects_verified_native_and_keeps_verified_cpu_fallback(monkeypatch):
    from plamo_cp import llama_backend, native_engine

    monkeypatch.setattr(runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_backend, "LlamaEngine", lambda: SimpleNamespace())
    monkeypatch.setattr(native_engine, "NativeEngine", lambda runtime: ("native", runtime))
    monkeypatch.setattr(native_runtime, "verified_native_runtime", lambda: None)
    assert not hasattr(runtime.make_engine(), "native_fallback_reason")
    spec = object()
    monkeypatch.setattr(native_runtime, "verified_native_runtime", lambda: spec)
    assert runtime.make_engine() == ("native", spec)

    def damaged():
        raise RuntimeError("Do not expose arbitrary paths from the native loader")

    monkeypatch.setattr(native_runtime, "verified_native_runtime", damaged)
    engine = runtime.make_engine()
    assert engine.native_fallback_reason == "GPU 実行環境を検証できないため CPU を使用します。"


def test_archive_preparation_checks_fixed_hash_preserves_notices_and_refuses_unrelated_output(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "official.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("build/bin/llama.dll", b"llama")
        output.writestr("build/bin/ggml-cpu.dll", b"cpu")
        output.writestr("build/bin/llama-server.exe", b"unused executable")
        output.writestr("LICENSE.txt", "upstream notices")
    asset = {"filename": "official.zip", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    monkeypatch.setitem(prepare_native_runtime.NATIVE_ASSETS, "x64", asset)
    target = tmp_path.resolve() / "native"
    prepare_native_runtime.prepare_runtime(target, architecture="x64", archive=archive)
    assert (target / "llama.dll").read_bytes() == b"llama"
    assert (target / "upstream/LICENSE.txt").read_text() == "upstream notices"
    assert not list(target.rglob("*.exe"))
    assert (
        json.loads((target / "upstream-runtime.json").read_text())["archive_sha256"]
        == asset["sha256"]
    )
    prepare_native_runtime.prepare_runtime(target, architecture="x64", archive=archive)
    asset["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256"):
        prepare_native_runtime.prepare_runtime(target, architecture="x64", archive=archive)
    assert (target / "llama.dll").read_bytes() == b"llama"
    unrelated = tmp_path.resolve() / "documents"
    unrelated.mkdir()
    (unrelated / "user.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="unrelated"):
        prepare_native_runtime.prepare_runtime(unrelated, architecture="x64", archive=archive)
    assert (unrelated / "user.txt").read_text() == "keep"


def test_build_copy_embeds_pins_without_overwriting_checkout_and_analysis_must_use_it(
    bundle, tmp_path
):
    _directory, pins = bundle
    root = tmp_path.resolve() / "source"
    source = root / "plamo_cp"
    source.mkdir(parents=True)
    (source / "_native_runtime_pins.py").write_text("RUNTIME_PINS = None\n")
    staged = prepare_native_runtime.stage_python_package(root, tmp_path.resolve() / "stage", pins)
    expected = staged / "plamo_cp/_native_runtime_pins.py"
    module = ast.parse(expected.read_text())
    assert ast.literal_eval(module.body[1].value) == pins
    assert (source / "_native_runtime_pins.py").read_text() == "RUNTIME_PINS = None\n"
    analysis = SimpleNamespace(pure=[("plamo_cp._native_runtime_pins", str(expected), "PYMODULE")])
    prepare_native_runtime.verify_analyzed_pins(analysis, expected)
    analysis.pure[0] = ("plamo_cp._native_runtime_pins", str(source / expected.name), "PYMODULE")
    with pytest.raises(RuntimeError, match="generated"):
        prepare_native_runtime.verify_analyzed_pins(analysis, expected)
    with pytest.raises(RuntimeError, match="overwrite"):
        prepare_native_runtime.stage_python_package(root, root, pins)


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *arguments):
        return self.callback(*arguments)


def test_model_free_smoke_checks_abi_cpu_backend_and_load_search_scope(bundle):
    directory, pins = bundle
    devices = [{"index": 0, "type": "cpu", "name": "CPU"}]
    raw = json.dumps(devices).encode()
    calls = []

    def buffer(_directory, output, capacity):
        assert _directory == str(directory).encode()
        if capacity:
            ctypes.memmove(output, raw, capacity)
        return len(raw)

    library = SimpleNamespace(
        cp_llama_abi_version=Function(lambda: 1),
        cp_llama_devices=Function(buffer),
        cp_llama_last_error=Function(lambda *_: 0),
    )

    def loader(path, **kwargs):
        calls.append((path, kwargs))
        return library

    assert smoke_native_runtime.smoke_runtime(directory, pins, loader=loader) == devices
    assert calls == [(str(directory / "cp_llama_bridge.dll"), {"winmode": 0x1100})]
    library.cp_llama_abi_version = Function(lambda: 2)
    with pytest.raises(RuntimeError, match="ABI"):
        smoke_native_runtime.smoke_runtime(directory, pins, loader=loader)
    # A trusted file must still be checked before reaching the DLL loader.
    altered = copy.deepcopy(pins)
    altered["files"]["llama.dll"]["sha256"] = "0" * 64
    calls.clear()
    with pytest.raises(RuntimeError):
        smoke_native_runtime.smoke_runtime(directory, altered, loader=loader)
    assert calls == []


@pytest.mark.parametrize("valid", [True, False])
def test_frozen_worker_smoke_requires_one_framed_failure_without_loading_a_model(tmp_path, valid):
    from plamo_cp.native_ipc import read_frame, write_frame

    executable = tmp_path / "PLaMo for 競プロ.exe"
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert read_frame(io.BytesIO(kwargs["input"])) == {"type": "smoke-invalid-init"}
        output = io.BytesIO()
        write_frame(output, {"type": "fatal", "message": "invalid init"})
        return SimpleNamespace(
            returncode=1, stdout=output.getvalue() + (b"log" if not valid else b"")
        )

    if valid:
        smoke_desktop_bundle.smoke_frozen_native_worker(
            executable, {"HF_HUB_OFFLINE": "1"}, run=run
        )
    else:
        with pytest.raises(RuntimeError, match="IPC"):
            smoke_desktop_bundle.smoke_frozen_native_worker(executable, {}, run=run)
    assert calls[0][0] == [str(executable.resolve()), "--native-worker"]
    assert calls[0][1]["timeout"] == 20

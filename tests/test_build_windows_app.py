from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest

from plamo_cp import llama_backend
from scripts import (
    build_llama_bridge,
    build_windows_app,
    prepare_native_runtime,
    smoke_native_runtime,
)


@pytest.mark.parametrize("mutate", [False, True])
def test_final_native_dlls_are_verified_before_archive_or_installer(tmp_path, monkeypatch, mutate):
    root = tmp_path.resolve()
    native = root / "prepared-native"
    native.mkdir()
    for name in (
        "cp_llama_bridge.dll",
        "llama.dll",
        "ggml.dll",
        "ggml-base.dll",
        "ggml-vulkan.dll",
        "ggml-cpu.dll",
    ):
        (native / name).write_bytes(name.encode())
    cpu = root / "cpu"
    cpu.mkdir()
    (cpu / ".runtime-manifest.json").write_text("{}")
    server = cpu / "llama-server.exe"
    server.write_bytes(b"fake executable; never run")
    output = root / "dist"
    final = output / "PLaMo for 競プロ/_internal/native"
    calls = []

    monkeypatch.setattr(build_windows_app, "ROOT", root)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        sys, "argv", ["build_windows_app", "--runtime-dir", str(cpu), "--output", str(output)]
    )
    original_find_spec = build_windows_app.importlib.util.find_spec
    monkeypatch.setattr(
        build_windows_app.importlib.util,
        "find_spec",
        lambda name: object() if name == "PyInstaller" else original_find_spec(name),
    )
    monkeypatch.setattr(llama_backend, "runtime_arch", lambda: "x64")
    monkeypatch.setattr(llama_backend, "_runtime_executable", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(prepare_native_runtime, "prepare_runtime", lambda *_args, **_kwargs: native)
    monkeypatch.setattr(
        build_llama_bridge, "build_bridge", lambda *_: native / "cp_llama_bridge.dll"
    )
    monkeypatch.setattr(smoke_native_runtime, "smoke_runtime", lambda *_: [{"type": "cpu"}])
    monkeypatch.setattr(
        prepare_native_runtime,
        "stage_python_package",
        lambda _root, destination, _pins: destination,
    )

    def run(arguments, **_kwargs):
        if arguments == [str(server), "--version"]:
            calls.append("version")
            return
        assert arguments[:3] == [sys.executable, "-m", "PyInstaller"]
        calls.append("pyinstaller")
        shutil.copytree(native, final)
        if mutate:
            # Keep the size identical so the actual hash must be checked.
            path = final / "llama.dll"
            path.write_bytes(b"x" * path.stat().st_size)

    monkeypatch.setattr(build_windows_app.subprocess, "run", run)
    monkeypatch.setattr(
        build_windows_app.shutil, "make_archive", lambda *_: calls.append("archive")
    )
    if mutate:
        with pytest.raises(RuntimeError, match="検証"):
            build_windows_app.main()
        assert calls == ["version", "pyinstaller"]
    else:
        build_windows_app.main()
        assert calls == ["version", "pyinstaller", "archive"]


def test_windows_exe_and_collect_explicitly_disable_binary_mutation():
    spec = Path(__file__).resolve().parents[1] / "packaging/windows/application.spec"
    module = ast.parse(spec.read_text(encoding="utf-8"))
    targets = {
        node.func.id: node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"EXE", "COLLECT"}
    }
    assert set(targets) == {"EXE", "COLLECT"}
    for node in targets.values():
        options = {keyword.arg: keyword.value for keyword in node.keywords}
        assert ast.literal_eval(options["upx"]) is False
        assert ast.literal_eval(options["strip"]) is False

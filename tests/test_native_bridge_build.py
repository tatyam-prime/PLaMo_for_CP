import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from scripts import build_llama_bridge as builder


def test_vendored_bridge_headers_are_exactly_pinned():
    manifest = builder.verify_headers()
    assert manifest["commit"] == "6f1e6543704bc5b080c7a98e7b6f707b31f6ed51"
    assert manifest["release"] == "b12119"
    assert all(
        item["url"].startswith(
            "https://raw.githubusercontent.com/ggml-org/llama.cpp/" + manifest["commit"] + "/"
        )
        for item in manifest["files"].values()
    )


def test_bridge_header_changes_and_symlinks_fail_before_compilation(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(builder.SOURCE / "vendor", source / "vendor")
    header = source / "vendor" / "llama.h"
    header.write_bytes(header.read_bytes() + b"\nchanged")
    with pytest.raises(RuntimeError, match="header changed"):
        builder.verify_headers(source)
    if sys.platform != "win32":
        header.unlink()
        header.symlink_to(builder.SOURCE / "vendor" / "llama.h")
        with pytest.raises(RuntimeError, match="missing or linked"):
            builder.verify_headers(source)


def test_bridge_build_rejects_other_platforms_before_invoking_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: pytest.fail("tool ran"))
    with pytest.raises(RuntimeError, match="x64 Windows"):
        builder.build_bridge(tmp_path)


def test_bridge_build_is_model_free_serial_and_emits_verifiable_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(builder.shutil, "which", lambda name: "C:/Build Tools/cmake.exe")
    output, build = tmp_path / "runtime 日本語", tmp_path / "compiler"
    commands = []

    def run(command, *, check):
        assert check is True
        commands.append(command)
        if "--build" in command:
            binary = build / "Release" / "cp_llama_bridge.dll"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"model-free compiler test output")

    monkeypatch.setattr(builder.subprocess, "run", run)
    binary = builder.build_bridge(output, build)
    assert binary == output / "cp_llama_bridge.dll"
    assert len(commands) == 2
    assert commands[0][-1] == "-DCP_LLAMA_BRIDGE_TESTS=OFF"
    assert commands[1][-2:] == ["--parallel", "1"]
    metadata = json.loads((output / "bridge-build.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert metadata["architecture"] == "x64" and metadata["abi"] == 1
    assert metadata["llama_commit"] == builder.UPSTREAM_COMMIT
    assert (output / "bridge-licenses" / "llama.cpp-LICENSE.txt").read_bytes() == (
        builder.SOURCE / "vendor" / "LICENSE"
    ).read_bytes()
    assert not list(output.glob("*.tmp"))
    with pytest.raises(ValueError, match="outside"):
        builder.build_bridge(output, output / "build")


def test_portable_bridge_abi_with_in_memory_backend_and_no_model(tmp_path):
    # Exercise the actual C ABI/core using fake function pointers. No llama DLL,
    # valid GGUF, networking, GPU, or Python llama-struct reimplementation exists.
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None or sys.platform == "win32":
        pytest.skip("Portable C++ compiler check; the Windows bridge has its own CI build")
    binary = tmp_path / "bridge-test"
    source = builder.SOURCE
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-UNDEBUG",
            str(source / "bridge.cpp"),
            str(source / "tests" / "fake_runtime.cpp"),
            str(source / "tests" / "bridge_test.cpp"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary), str(tmp_path)], check=True, capture_output=True, timeout=10)

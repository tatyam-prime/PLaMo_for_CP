"""Build the pinned C ABI bridge on Windows without downloading a runtime or model."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "llama_bridge"
UPSTREAM_COMMIT = "6f1e6543704bc5b080c7a98e7b6f707b31f6ed51"
ABI_VERSION = 1


def verify_headers(source: Path = SOURCE) -> dict:
    """Verify the exact vendored public headers, including the upstream license."""
    vendor = source / "vendor"
    manifest = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
    if manifest.get("commit") != UPSTREAM_COMMIT or manifest.get("release") != "b12119":
        raise RuntimeError("The llama.cpp header pin does not match the runtime pin.")
    expected = {
        "llama.h",
        "ggml.h",
        "ggml-cpu.h",
        "ggml-backend.h",
        "ggml-opt.h",
        "gguf.h",
        "ggml-alloc.h",
        "LICENSE",
    }
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected:
        raise RuntimeError("The pinned public-header inventory is incomplete.")
    for name, item in files.items():
        path = vendor / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"The pinned header is missing or linked: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            raise RuntimeError(f"The pinned header changed: {name}")
    return manifest


def build_bridge(output_dir: Path, build_dir: Path | None = None) -> Path:
    """Return output_dir/cp_llama_bridge.dll; all compilation stays in build_dir."""
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build the x64 Windows bridge on an x64 Windows build machine.")
    verify_headers()
    output_dir = Path(output_dir).resolve()
    build_dir = Path(build_dir or ROOT / ".local" / "llama-bridge-build").resolve()
    if build_dir == output_dir or build_dir.is_relative_to(output_dir):
        raise ValueError("Keep the compiler build directory outside the packaged runtime.")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise RuntimeError(
            "CMake and the Visual Studio C++ build tools are required on the build machine."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            cmake,
            "-S",
            str(SOURCE),
            "-B",
            str(build_dir),
            "-A",
            "x64",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCP_LLAMA_BRIDGE_TESTS=OFF",
        ],
        check=True,
    )
    subprocess.run(
        [cmake, "--build", str(build_dir), "--config", "Release", "--parallel", "1"],
        check=True,
    )
    binary = build_dir / "Release" / "cp_llama_bridge.dll"
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeError("The compiler did not produce the bridge DLL.")
    target = output_dir / "cp_llama_bridge.dll"
    temporary = output_dir / ".cp_llama_bridge.dll.tmp"
    shutil.copyfile(binary, temporary)
    temporary.replace(target)
    notices = output_dir / "bridge-licenses"
    notices.mkdir(exist_ok=True)
    shutil.copyfile(SOURCE / "vendor" / "LICENSE", notices / "llama.cpp-LICENSE.txt")
    shutil.copyfile(SOURCE / "vendor" / "provenance.json", notices / "llama.cpp-provenance.json")
    provenance = {
        "schema": 1,
        "abi": ABI_VERSION,
        "architecture": "x64",
        "llama_release": "b12119",
        "llama_commit": UPSTREAM_COMMIT,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "sources": {
            name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
            for name in (
                "bridge.h",
                "bridge.cpp",
                "runtime.h",
                "runtime_windows.cpp",
                "CMakeLists.txt",
            )
        },
        "header_manifest_sha256": hashlib.sha256(
            (SOURCE / "vendor" / "provenance.json").read_bytes()
        ).hexdigest(),
    }
    metadata = output_dir / ".bridge-build.json.tmp"
    metadata.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    metadata.replace(output_dir / "bridge-build.json")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "windows-native-runtime")
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    try:
        print(build_bridge(args.output, args.build_dir))
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

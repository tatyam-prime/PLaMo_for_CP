"""Build a Windows app with bundled Python/Tk; run on Windows itself."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    # Windows のリダイレクト先が cp1252 でも、日本語の成果物パスを保持する。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".local/windows-runtime/llama")
    parser.add_argument(
        "--native-runtime-dir", type=Path, default=ROOT / ".local/windows-native-runtime"
    )
    parser.add_argument(
        "--without-native", action="store_true", help="Build only the verified CPU-server fallback"
    )
    parser.add_argument(
        "--installer", action="store_true", help="Also build a per-user installer with Inno Setup"
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build the Windows application on Windows.")
    if importlib.util.find_spec("PyInstaller") is None:
        parser.error("PyInstaller is required on the build machine.")
    if not (args.runtime_dir / ".runtime-manifest.json").is_file():
        parser.error("Prepare the pinned llama.cpp runtime first; see packaging/windows/README.md.")
    from plamo_cp.llama_backend import _runtime_executable

    runtime_executable = _runtime_executable(args.runtime_dir.resolve(), verify=True)
    if runtime_executable is None:
        parser.error("The pinned Windows inference runtime failed verification.")
    # A model-free launch catches missing DLLs/VC runtime on the build machine.
    subprocess.run([str(runtime_executable), "--version"], check=True, timeout=30)
    build = ROOT / ".local/windows-build"
    environment = dict(os.environ, PLAMO_WINDOWS_RUNTIME_DIR=str(args.runtime_dir.resolve()))
    environment.pop("PLAMO_NATIVE_RUNTIME_DIR", None)
    environment.pop("PLAMO_STAGED_PACKAGE_ROOT", None)
    pins = None
    if not args.without_native:
        from plamo_cp.llama_backend import runtime_arch
        from scripts.build_llama_bridge import build_bridge
        from scripts.prepare_native_runtime import (
            create_pins,
            prepare_runtime,
            stage_python_package,
        )
        from scripts.smoke_native_runtime import smoke_runtime

        native = prepare_runtime(args.native_runtime_dir, architecture=runtime_arch())
        build_bridge(native, build / "bridge")
        pins = create_pins(native, runtime_arch())
        # No weights or inference: ensure exact ABI and CPU device discovery
        # work even on a Windows CI runner without a Vulkan-capable GPU.
        smoke_runtime(native, pins)
        staged = stage_python_package(ROOT, build / "staged-source", pins)
        environment["PLAMO_NATIVE_RUNTIME_DIR"] = str(native)
        environment["PLAMO_STAGED_PACKAGE_ROOT"] = str(staged)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(args.output),
            "--workpath",
            str(build),
            str(ROOT / "packaging/windows/application.spec"),
        ],
        cwd=ROOT,
        check=True,
        env=environment,
    )
    application = args.output.absolute() / "PLaMo for 競プロ"
    if pins is not None:
        from plamo_cp.native_runtime import verify_native_runtime_directory

        # PyInstaller reclassifies DLL data as binaries and may collect their
        # dependencies. Check the actual output against the original embedded
        # pins before distributing it; never regenerate trust from final bytes.
        verify_native_runtime_directory(application / "_internal/native", pins)
    shutil.make_archive(
        str(args.output / "PLaMo-for-競プロ-Windows"), "zip", args.output, application.name
    )
    print(application / "PLaMo for 競プロ.exe")
    if args.installer:
        compiler = shutil.which("ISCC.exe")
        if compiler is None:
            candidate = (
                Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
                / "Inno Setup 6/ISCC.exe"
            )
            compiler = str(candidate) if candidate.is_file() else None
        if compiler is None:
            parser.error("Inno Setup 6 is required to build the installer.")
        subprocess.run(
            [
                compiler,
                f"/DAppSource={application.resolve()}",
                f"/DAppOutput={args.output.resolve()}",
                str(ROOT / "packaging/windows/installer.iss"),
            ],
            check=True,
            cwd=ROOT,
        )
        print(args.output / "PLaMo-for-競プロ-Windows-Setup.exe")


if __name__ == "__main__":
    main()

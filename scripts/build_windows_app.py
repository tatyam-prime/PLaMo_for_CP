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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".local/windows-runtime/llama")
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
    os.environ["PLAMO_WINDOWS_RUNTIME_DIR"] = str(args.runtime_dir.resolve())
    build = ROOT / ".local/windows-build"
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
    )
    application = args.output / "PLaMo for CP"
    shutil.make_archive(
        str(args.output / "PLaMo-for-CP-Windows"), "zip", args.output, application.name
    )
    print(application / "PLaMo for CP.exe")
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
        print(args.output / "PLaMo-for-CP-Windows-Setup.exe")


if __name__ == "__main__":
    main()

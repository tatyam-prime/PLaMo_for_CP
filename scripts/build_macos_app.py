"""Build an Apple Silicon .app with its own Python and local inference runtime.

This developer build requires PyInstaller and Xcode command-line tools. It never
fetches model weights or installs build dependencies automatically.
"""

from __future__ import annotations

import argparse
import importlib.util
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def minimum_version_from_otool(output: str) -> str:
    versions = re.findall(r"(?m)^\s*minos\s+([0-9.]+)\s*$", output)
    versions += re.findall(r"LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([0-9.]+)", output)
    parsed = [tuple(map(int, value.split("."))) for value in versions]
    maximum = max([(14, 0), *parsed])
    return ".".join(map(str, maximum))


def minimum_macos_version(backend: Path) -> str:
    # A launcher compiled for an older OS cannot lower the deployment target of
    # an embedded Python/framework or MLX dylib. State the real requirement.
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
    binaries = []
    seen = set()
    for path in backend.rglob("*"):
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        with path.open("rb") as source:
            if source.read(4) in magic:
                binaries.append(str(path))
    output = subprocess.check_output(["otool", "-l", *binaries], text=True)
    return minimum_version_from_otool(output)


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    import platform
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "macos")
    parser.add_argument(
        "--skip-backend", action="store_true", help="Reuse an existing frozen backend"
    )
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("Build this application on an Apple Silicon Mac.")
    if not args.skip_backend and importlib.util.find_spec("PyInstaller") is None:
        parser.error(
            "Install the capped packaging dependencies first; see packaging/macos/README.md."
        )
    build = ROOT / ".local" / "macos-build"
    build.mkdir(parents=True, exist_ok=True)
    backend_root = build / "dist"
    if not args.skip_backend:
        run(
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(backend_root),
            "--workpath",
            str(build / "work"),
            str(ROOT / "packaging/macos/backend.spec"),
        )
    backend = backend_root / "plamo-cp-backend"
    if not (backend / "plamo-cp-backend").is_file():
        parser.error("The frozen backend has not been built.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = output / "PLaMo for CP.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    icon = ROOT / "packaging/icons/app-icon.icns"
    if not icon.is_file():
        parser.error("Generate packaging/icons/app-icon.icns with scripts/build_app_icons.py.")
    shutil.copyfile(icon, contents / "Resources/app-icon.icns")
    shutil.copytree(backend, contents / "Resources" / "backend", symlinks=True)
    # UI/userscript resources can change after the Python analysis completes.
    # Always stage the current files before signing, including --skip-backend.
    package_assets = contents / "Resources/backend/_internal/plamo_cp/assets"
    if package_assets.exists():
        shutil.rmtree(package_assets)
    package_assets.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "plamo_cp/assets", package_assets, dirs_exist_ok=True)
    userscript = ROOT / "dist/plamo-for-cp.user.js"
    if userscript.is_file():
        shutil.copyfile(userscript, package_assets / "plamo-for-cp.user.js")
    minimum = minimum_macos_version(backend)
    launcher = contents / "MacOS" / "PLaMo for CP"
    run(
        "xcrun",
        "swiftc",
        "-swift-version",
        "5",
        "-O",
        "-target",
        f"arm64-apple-macos{minimum}",
        "-framework",
        "AppKit",
        str(ROOT / "packaging/macos/BoundedLog.swift"),
        str(ROOT / "packaging/macos/HealthProbe.swift"),
        str(ROOT / "packaging/macos/launcher.swift"),
        "-o",
        str(launcher),
    )
    info = {
        "CFBundleName": "PLaMo for CP",
        "CFBundleDisplayName": "PLaMo for CP",
        "CFBundleIdentifier": "jp.plamo-for-cp.desktop",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "PLaMo for CP",
        "CFBundleIconFile": "app-icon.icns",
        "LSMinimumSystemVersion": minimum,
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    run(str(launcher), "--check-bundle")
    archive = output / "PLaMo-for-CP-macOS-AppleSilicon.zip"
    if archive.exists():
        archive.unlink()
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive))
    print(app)
    print(archive)
    print(f"Minimum macOS version of the bundled binaries: {minimum}")


if __name__ == "__main__":
    main()

"""Build-time preparation of pinned Vulkan DLLs and frozen Python trust pins.

No application startup calls this module. Windows CI explicitly downloads the
fixed archive, builds the bridge, then embeds these file hashes as Python code.
"""

from __future__ import annotations

import argparse
import json
import pprint
import shutil
import tempfile
from pathlib import Path

import httpx

from plamo_cp.download import BandwidthLimiter, limited_client_factory, resolve_rate
from plamo_cp.llama_backend import _safe_extract, _sha256, runtime_arch
from plamo_cp.native_runtime import (
    BRIDGE_ABI,
    NATIVE_RELEASE,
    _regular_tree,
    runtime_file_pins,
    verify_native_runtime_directory,
)

NATIVE_ASSETS = {
    "x64": {
        "filename": "llama-b12119-bin-win-vulkan-x64.zip",
        "sha256": "da6f3e49f5b2383bc79dd9bfcb17205e69df4aaa8ccf72d3db02da72cdb5e89a",
    },
    "arm64": {
        "filename": "llama-b12119-bin-win-vulkan-arm64.zip",
        "sha256": "6fb017b9ce340e0303122bd776d7c7c465953b51626f4a888affcf558838667b",
    },
}
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _replaceable_directory(directory: Path, marker: str) -> None:
    """Never recursively replace an arbitrary existing developer directory."""
    if directory != directory.resolve():
        raise RuntimeError("The native build output must not contain links.")
    if directory.exists():
        _regular_tree(directory)
        if any(directory.iterdir()) and not (directory / marker).is_file():
            raise RuntimeError("Refusing to replace an unrelated build directory.")


def _archive(cache_dir: Path, architecture: str) -> Path:
    asset = NATIVE_ASSETS[architecture]
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / asset["filename"]
    if target.is_file() and _sha256(target) == asset["sha256"]:
        return target
    partial = target.with_suffix(".part")
    client = limited_client_factory(BandwidthLimiter(resolve_rate()))
    client.timeout = httpx.Timeout(30, connect=10)
    url = f"https://github.com/ggml-org/llama.cpp/releases/download/{NATIVE_RELEASE}/{asset['filename']}"
    try:
        with client, client.stream("GET", url) as response:
            response.raise_for_status()
            received = 0
            with partial.open("wb") as output:
                for chunk in response.iter_bytes(64 * 1024):
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("The native archive exceeds its download bound.")
                    output.write(chunk)
        if _sha256(partial) != asset["sha256"]:
            raise RuntimeError("The native Vulkan archive failed its fixed SHA-256 check.")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def prepare_runtime(
    directory: Path,
    *,
    architecture: str | None = None,
    archive: Path | None = None,
    cache_dir: Path | None = None,
) -> Path:
    """Verify an official archive, flatten DLLs and preserve all other notices."""
    architecture = architecture or runtime_arch()
    if architecture not in NATIVE_ASSETS:
        raise RuntimeError("Unsupported native Windows runtime architecture.")
    directory = directory.absolute()
    _replaceable_directory(directory, "upstream-runtime.json")
    asset = NATIVE_ASSETS[architecture]
    archive = archive or _archive(cache_dir or directory.parent / "archives", architecture)
    if archive.resolve().is_relative_to(directory):
        raise RuntimeError("Keep the original runtime ZIP outside the extracted directory.")
    if _sha256(archive) != asset["sha256"]:
        raise RuntimeError("The native Vulkan archive failed its fixed SHA-256 check.")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        extracted, staged = Path(temporary) / "extracted", Path(temporary) / "native"
        extracted.mkdir()
        staged.mkdir()
        inventory = _safe_extract(archive, extracted)
        dll_names = set()
        for name in inventory:
            source = extracted / name
            if source.suffix.casefold() == ".dll":
                identity = source.name.casefold()
                if identity in dll_names:
                    raise RuntimeError("The runtime archive contains duplicate DLL basenames.")
                dll_names.add(identity)
                target = staged / source.name
            elif source.suffix.casefold() == ".exe":
                # The separately pinned CPU server remains the fallback. The
                # bridge bundle does not need duplicate server/CLI executables.
                continue
            else:
                target = staged / "upstream" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        provenance = {
            "llama_release": NATIVE_RELEASE,
            "architecture": architecture,
            "archive": asset["filename"],
            "archive_sha256": asset["sha256"],
            "source": f"https://github.com/ggml-org/llama.cpp/releases/tag/{NATIVE_RELEASE}",
        }
        (staged / "upstream-runtime.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if directory.exists():
            shutil.rmtree(directory)
        staged.replace(directory)
    return directory


def create_pins(directory: Path, architecture: str) -> dict:
    """Called only after the verified archive and locally built bridge are staged."""
    pins = {
        "schema": 1,
        "llama_release": NATIVE_RELEASE,
        "architecture": architecture,
        "bridge_abi": BRIDGE_ABI,
        "files": runtime_file_pins(directory),
    }
    verify_native_runtime_directory(directory, pins)
    return pins


def stage_python_package(root: Path, destination: Path, pins: dict) -> Path:
    """Inject pins into a private build copy, preserving source checkout settings."""
    package = destination / "plamo_cp"
    if (
        destination.resolve() == root.resolve()
        or package.resolve() == (root / "plamo_cp").resolve()
    ):
        raise RuntimeError("The staged package must not overwrite the source checkout.")
    _replaceable_directory(package.absolute(), "_native_runtime_pins.py")
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(
        root / "plamo_cp", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (package / "_native_runtime_pins.py").write_text(
        '"""Generated build-time native trust pins, embedded into frozen code."""\n\n'
        + "RUNTIME_PINS = "
        + pprint.pformat(pins, sort_dicts=True, width=100)
        + "\n",
        encoding="utf-8",
    )
    return destination


def verify_analyzed_pins(analysis, expected_source: Path) -> None:
    sources = [
        Path(source).resolve()
        for name, source, _kind in analysis.pure
        if name == "plamo_cp._native_runtime_pins"
    ]
    if sources != [expected_source.resolve()]:
        raise RuntimeError("The frozen application did not analyze its generated native pins.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--architecture", choices=tuple(NATIVE_ASSETS), default=None)
    args = parser.parse_args()
    print(prepare_runtime(args.output, architecture=args.architecture))


if __name__ == "__main__":
    main()

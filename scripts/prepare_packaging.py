"""Fetch macOS build tools sequentially under the model-download bandwidth cap.

Run from the project environment. These are developer tools, never dependencies
that an end user must install. The optional installation uses only local wheels.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

from plamo_cp.download import BandwidthLimiter, limited_client_factory

PACKAGES = {
    "pyinstaller": "6.22.3",
    "pyinstaller-hooks-contrib": "2026.9",
    "altgraph": "0.17.5",
    "macholib": "1.16.4",
    "setuptools": "84.2.1",
}


def prepare(output: Path, rate_mib: float) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    tags = set(sys_tags())
    wheels = []
    with limited_client_factory(BandwidthLimiter(rate_mib)) as client:
        for name, version in PACKAGES.items():
            metadata = output / f"{name}.json"
            data = json.loads(metadata.read_text()) if metadata.is_file() else None
            if data is None or data.get("version") != version:
                response = client.get(f"https://pypi.org/pypi/{name}/{version}/json")
                response.raise_for_status()
                payload = response.json()
                data = {
                    "name": name,
                    "version": payload["info"]["version"],
                    "requires_dist": payload["info"]["requires_dist"],
                    "files": [
                        {
                            "filename": item["filename"],
                            "url": item["url"],
                            "sha256": item["digests"]["sha256"],
                            "size": item["size"],
                        }
                        for item in payload["urls"]
                    ],
                }
                metadata.write_text(json.dumps(data, indent=2) + "\n")
            candidates = [
                item
                for item in data["files"]
                if item["filename"].endswith(".whl")
                and parse_wheel_filename(item["filename"])[3] & tags
            ]
            if not candidates:
                raise RuntimeError(f"No compatible wheel found: {name} {version}")
            item = candidates[0]
            wheel = output / item["filename"]
            if (
                not wheel.is_file()
                or hashlib.sha256(wheel.read_bytes()).hexdigest() != item["sha256"]
            ):
                temporary = wheel.with_suffix(".part")
                digest = hashlib.sha256()
                with client.stream("GET", item["url"]) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as target:
                        for chunk in response.iter_bytes():
                            digest.update(chunk)
                            target.write(chunk)
                if digest.hexdigest() != item["sha256"]:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"SHA-256 mismatch: {wheel.name}")
                temporary.replace(wheel)
            wheels.append(wheel)
            print(f"Ready: {wheel.name} ({wheel.stat().st_size:,} bytes)", flush=True)
    return wheels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/packaging-wheels"))
    parser.add_argument("--rate-mib", type=float, default=0.5)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    prepare(args.output, args.rate_mib)
    if args.install:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--no-index",
                "--find-links",
                str(args.output),
                f"pyinstaller=={PACKAGES['pyinstaller']}",
            ],
            check=True,
        )
        print(f"Installed PyInstaller {importlib.metadata.version('pyinstaller')}")


if __name__ == "__main__":
    main()

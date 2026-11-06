"""Convert the source PNG into committed macOS/Windows icons without dependencies.

Run on macOS (sips/iconutil). Windows builds consume the generated ICO directly.
Only resizing and format conversion are performed; alpha is preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICONSET = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def canonical_png(data: bytes) -> bytes:
    """Remove volatile textual/EXIF timestamps while retaining color and alpha."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("The source or resized file is not a PNG.")
    chunks = [PNG_SIGNATURE]
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        if end > len(data):
            raise ValueError("Truncated PNG chunk.")
        kind = data[offset + 4 : offset + 8]
        if kind not in {b"tIME", b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
            chunks.append(data[offset:end])
        offset = end
        if kind == b"IEND":
            break
    return b"".join(chunks)


def make_ico(images: dict[int, bytes]) -> bytes:
    offset = 6 + 16 * len(ICO_SIZES)
    entries, payloads = [], []
    for size in ICO_SIZES:
        data = images[size]
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        )
        payloads.append(data)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(ICO_SIZES)) + b"".join(entries + payloads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "packaging/icons/app-icon.png")
    parser.add_argument("--output", type=Path, default=ROOT / "packaging/icons")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Generate icons on macOS; Windows builds use the committed .ico.")
    source = args.source.resolve()
    original = source.read_bytes()
    if not original.startswith(PNG_SIGNATURE):
        parser.error("The source must be a PNG.")
    width, height = struct.unpack_from(">II", original, 16)
    if width != height or width < 1024 or original[25] != 6:
        parser.error("Use a square RGBA PNG of at least 1024 pixels.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plamo-app-icons-") as temporary:
        work = Path(temporary)
        images = {}
        for size in sorted(set(ICO_SIZES) | set(ICONSET.values())):
            resized = work / f"{size}.png"
            subprocess.run(
                [
                    "sips",
                    "--resampleHeightWidth",
                    str(size),
                    str(size),
                    str(source),
                    "--out",
                    str(resized),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            images[size] = canonical_png(resized.read_bytes())
        iconset = work / "app-icon.iconset"
        iconset.mkdir()
        for name, size in ICONSET.items():
            (iconset / name).write_bytes(images[size])
        icns = work / "app-icon.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        (output / "app-icon.icns").write_bytes(icns.read_bytes())
        (output / "app-icon.ico").write_bytes(make_ico(images))
        destination = output / iconset.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(iconset, destination)
    print(
        json.dumps(
            {
                "source_sha256": hashlib.sha256(original).hexdigest(),
                "icns_sha256": hashlib.sha256((output / "app-icon.icns").read_bytes()).hexdigest(),
                "ico_sha256": hashlib.sha256((output / "app-icon.ico").read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

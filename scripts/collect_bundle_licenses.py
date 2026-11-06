"""Preserve license texts for the exact inputs selected by PyInstaller Analysis.

Call ``analysis.datas += collect_bundle_licenses(analysis, build_dir,
supplemental_dir=root / 'packaging/licenses')`` after Analysis. Returned records
are PyInstaller TOC triples, not the two-item input format of Analysis(datas=).
No dependency imports, downloads, or license-text paraphrases are performed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path, PurePosixPath

DESTINATION = "third_party_licenses"
NOTICE_NAME = re.compile(
    r"(?:^|[^a-z])(?:licen[cs]es?|copying|copyright|notices?)(?:$|[^a-z])", re.IGNORECASE
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-") or "component"


def _notice(path: Path | PurePosixPath) -> bool:
    return bool(NOTICE_NAME.search(path.name)) or "licenses" in {
        part.casefold() for part in path.parts
    }


def _sources(analysis) -> tuple[dict[Path, set[str]], list[tuple[str, Path]]]:
    sources: dict[Path, set[str]] = {}
    binaries = []
    for table in ("pure", "binaries", "datas", "scripts"):
        for target, source, _kind in getattr(analysis, table, ()):
            if not source:
                continue
            path = Path(source).resolve()
            sources.setdefault(path, set()).add(str(target))
            if table == "binaries":
                binaries.append((str(target), path))
    return sources, binaries


def _stdlib_license() -> Path:
    for path in (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ):
        if path.is_file():
            return path
    raise RuntimeError("The Python interpreter's complete LICENSE text was not found.")


def _native_component(name: str) -> str | None:
    name = name.lower()
    if ".cpython-" in name or name.endswith(".pyd"):
        return None
    for prefix, component in (
        ("libssl", "openssl"),
        ("libcrypto", "openssl"),
        ("liblzma", "xz"),
        ("libmpdec", "mpdecimal"),
        ("libsqlite", "sqlite"),
        ("sqlite3.", "sqlite"),
        ("libffi", "libffi"),
        ("libbz2", "bzip2"),
        ("zlib", "zlib"),
        ("_tcl", "tcl"),
        ("_tk", "tk"),
        ("tcl", "tcl"),
        ("tk8", "tk"),
        ("tk9", "tk"),
    ):
        if name.startswith(prefix):
            return component
    return None


def collect_bundle_licenses(
    analysis,
    output_dir: str | Path,
    *,
    supplemental_dir: str | Path | None = None,
    distributions=None,
    python_license: str | Path | None = None,
    strict: bool = True,
) -> list[tuple[str, str, str]]:
    """Write full notices plus a deterministic, portable SHA256 provenance index.

    ``supplemental_dir/<distribution-name>-<version>/`` supplies upstream texts
    omitted by a wheel. Native components can use ``<component>/``. Supplemental
    trees must contain a LICENSE/COPYING/NOTICE file; provenance.json alone is
    insufficient. Missing notices fail the build by default, after writing INDEX.
    ``strict=False`` is intended for an inventory audit, never a release build.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_targets, binaries = _sources(analysis)
    distributions = list(metadata.distributions() if distributions is None else distributions)
    records, owned, copied, missing = [], set(), {}, []
    supplements = Path(supplemental_dir) if supplemental_dir is not None else None

    def preserve(path: Path, relative: str, *, origin: str) -> dict:
        safe = PurePosixPath(relative)
        if safe.is_absolute() or ".." in safe.parts:
            raise ValueError("License destination must remain inside the bundle notice directory.")
        data = path.read_bytes()
        destination = output_dir.joinpath(*safe.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        copied[safe.as_posix()] = destination
        return {
            "path": safe.as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "origin": origin,
        }

    def supplemental(component: str, version: str = "") -> list[tuple[Path, str]]:
        if supplements is None:
            return []
        exact = supplements / f"{_slug(component)}-{_slug(version)}" if version else None
        directory = (
            exact if exact is not None and exact.is_dir() else supplements / _slug(component)
        )
        if not directory.is_dir():
            return []
        paths = sorted(path for path in directory.rglob("*") if path.is_file())
        if not any(_notice(path.relative_to(directory)) for path in paths):
            return []
        if directory.is_symlink() or any(
            not path.resolve().is_relative_to(directory.resolve()) for path in paths
        ):
            raise ValueError(
                f"Supplemental license directory contains an external link: {component}"
            )
        return [(path, path.relative_to(directory).as_posix()) for path in paths]

    for distribution in distributions:
        name = str(distribution.metadata.get("Name") or "")
        version = str(distribution.version)
        files = list(distribution.files or ())
        matches = {
            str(target)
            for file in files
            for target in source_targets.get(Path(distribution.locate_file(file)).resolve(), ())
        }
        bootloader = _slug(name) == "pyinstaller"
        if not matches and not bootloader:
            continue
        for file in files:
            owned.add(Path(distribution.locate_file(file)).resolve())
        prefix = f"python/{_slug(name)}-{_slug(version)}"
        notices = []
        metadata_file = None
        for file in files:
            relative = PurePosixPath(str(file).replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            path = Path(distribution.locate_file(file))
            if not path.is_file():
                continue
            if _notice(relative):
                notices.append(preserve(path, f"{prefix}/{relative}", origin="installed-wheel"))
            elif relative.name == "METADATA" and any(
                part.endswith(".dist-info") for part in relative.parts
            ):
                metadata_file = preserve(path, f"{prefix}/METADATA", origin="installed-wheel")
        for path, relative in supplemental(name, version):
            notices.append(preserve(path, f"{prefix}/upstream/{relative}", origin="supplemental"))
        if not notices:
            missing.append(f"Python distribution {name} {version}")
        records.append(
            {
                "name": name,
                "version": version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "bundled_targets": sorted(matches),
                "bootloader": bootloader,
                "files": notices,
                "metadata": metadata_file,
            }
        )

    if not any(record["bootloader"] for record in records):
        missing.append("PyInstaller bootloader GPL license and exception")
    python_path = Path(python_license) if python_license is not None else _stdlib_license()
    python_notice = preserve(python_path, "python-stdlib/LICENSE.txt", origin="python-interpreter")
    native_records = []
    analyzed_notices: dict[str, list[dict]] = {}
    for target, source, _kind in getattr(analysis, "datas", ()):
        path = Path(source).resolve()
        relative = PurePosixPath(str(target).replace("\\", "/"))
        if path in owned or not path.is_file() or not _notice(relative):
            continue
        # Tcl/Tk hooks collect their own license.terms with the data trees.
        component = next(
            (_native_component(part) for part in relative.parts if _native_component(part)), None
        )
        if component is None:
            continue
        value = preserve(path, f"native/analyzed/{relative}", origin="analyzed-data")
        analyzed_notices.setdefault(component, []).append(value)
    handled = set()
    for target, binary in binaries:
        if binary in owned:
            continue
        component = _native_component(binary.name)
        if component is None:
            continue  # CPython extension modules are covered by the interpreter license.
        identity = (component, binary.parent)
        if identity in handled:
            continue
        handled.add(identity)
        notices = list(analyzed_notices.get(component, ()))
        prefix = f"native/{component}"
        # Homebrew and similar library installs keep full notices at their
        # versioned prefix. Do not mistake Python's LICENSE for an SSL/Tcl one.
        for parent in list(binary.parents)[:3]:
            if parent in {
                Path(sys.base_prefix).resolve(),
                Path(sysconfig.get_path("stdlib")).resolve(),
            }:
                break
            candidates = sorted(
                path for path in parent.iterdir() if path.is_file() and _notice(path)
            )
            if candidates:
                notices.extend(
                    preserve(path, f"{prefix}/{path.name}", origin="native-prefix")
                    for path in candidates
                )
                break
            if component == "sqlite" and (parent / "include/sqlite3.h").is_file():
                header = parent / "include/sqlite3.h"
                data = header.read_bytes().split(
                    b"*************************************************************************", 1
                )[0]
                if b"disclaims copyright" in data and b"blessing" in data:
                    notice = output_dir / prefix / "sqlite3-header-notice.txt"
                    notice.parent.mkdir(parents=True, exist_ok=True)
                    notice.write_bytes(data + b"*/\n")
                    notices.append(
                        preserve(
                            notice,
                            f"{prefix}/sqlite3-header-notice.txt",
                            origin="installed-sqlite3.h",
                        )
                    )
                    break
        for path, relative in supplemental(component):
            notices.append(preserve(path, f"{prefix}/upstream/{relative}", origin="supplemental"))
        if not notices:
            missing.append(f"Native library {component}: {binary.name}")
        native_records.append({"name": component, "bundled_binary": target, "files": notices})

    index = {
        "format_version": 1,
        "python": {"version": sys.version.split()[0], "files": [python_notice]},
        "distributions": sorted(records, key=lambda record: record["name"].lower()),
        "native_libraries": native_records,
        "missing_license_texts": missing,
    }
    index_path = output_dir / "INDEX.json"
    index_path.write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    copied["INDEX.json"] = index_path
    if strict and missing:
        raise RuntimeError("Missing bundle license texts: " + "; ".join(missing))
    return [
        (f"{DESTINATION}/{relative}", str(path), "DATA")
        for relative, path in sorted(copied.items())
    ]

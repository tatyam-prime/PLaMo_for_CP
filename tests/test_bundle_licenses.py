"""Build-only license collection preserves actual analyzed inputs, without network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from scripts.collect_bundle_licenses import collect_bundle_licenses


class Distribution:
    def __init__(self, root, name, files, version="1.2.3"):
        self.root, self.version = root, version
        self.metadata = {"Name": name, "License-Expression": "MIT"}
        self.files = [PurePosixPath(file) for file in files]
        for file, content in files.items():
            path = self.root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def locate_file(self, file):
        return self.root / file


@pytest.fixture
def inventory(tmp_path):
    root = tmp_path / "installed"
    package = Distribution(
        root,
        "example",
        {
            "example/__init__.py": b"pass\n",
            "example-1.2.3.dist-info/METADATA": b"Name: example\nVersion: 1.2.3\n",
            "example-1.2.3.dist-info/licenses/LICENSE": b"full package license\n",
            "example-1.2.3.dist-info/licenses/vendor/NOTICE": b"vendored attribution\n",
            "example/LICENSES.third-party": b"additional third party text\n",
        },
    )
    unused = Distribution(
        root,
        "not-bundled",
        {
            "unrelated/__init__.py": b"pass\n",
            "unrelated/LICENSE": b"do not copy\n",
        },
    )
    bundler = Distribution(
        root,
        "PyInstaller",
        {
            "pyinstaller.dist-info/licenses/COPYING.txt": b"GPL full text and Bootloader Exception\n",
        },
        version="6.0",
    )
    python = tmp_path / "python" / "LICENSE.txt"
    python.parent.mkdir()
    python.write_bytes(b"complete Python license including historical notices\n")
    analysis = SimpleNamespace(
        pure=[("example", str(root / "example/__init__.py"), "PYMODULE")],
        binaries=[],
        datas=[],
        scripts=[],
    )
    return SimpleNamespace(
        root=root,
        package=package,
        unused=unused,
        bundler=bundler,
        python=python,
        analysis=analysis,
        output=tmp_path / "output",
        distributions=[package, unused, bundler],
    )


def collect(inventory, **options):
    return collect_bundle_licenses(
        inventory.analysis,
        inventory.output,
        distributions=inventory.distributions,
        python_license=inventory.python,
        **options,
    )


def index(inventory):
    return json.loads((inventory.output / "INDEX.json").read_text())


def test_full_license_trees_metadata_python_and_bootloader_are_preserved(inventory):
    records = collect(inventory)
    value = index(inventory)
    assert {item["name"] for item in value["distributions"]} == {"example", "PyInstaller"}
    assert value["missing_license_texts"] == []
    package = next(item for item in value["distributions"] if item["name"] == "example")
    assert len(package["files"]) == 3
    assert package["metadata"] is not None
    assert package["bundled_targets"] == ["example"]
    for item in [*package["files"], package["metadata"], *value["python"]["files"]]:
        data = (inventory.output / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    assert all(
        kind == "DATA"
        and destination.startswith("third_party_licenses/")
        and Path(source).is_file()
        for destination, source, kind in records
    )
    assert "Bootloader Exception" in "".join(
        Path(source).read_text()
        for _destination, source, _kind in records
        if source.endswith("COPYING.txt")
    )
    assert str(inventory.root) not in (inventory.output / "INDEX.json").read_text()


def test_distribution_ownership_uses_binaries_and_data_not_just_module_names(inventory):
    inventory.analysis.pure = []
    inventory.analysis.binaries = [
        ("renamed.so", str(inventory.root / "example/__init__.py"), "BINARY")
    ]
    assert len(index_after_collect(inventory)["distributions"]) == 2
    inventory.analysis.binaries = []
    inventory.analysis.datas = [
        ("resources/example", str(inventory.root / "example/LICENSES.third-party"), "DATA")
    ]
    assert len(index_after_collect(inventory)["distributions"]) == 2


def index_after_collect(inventory):
    collect(inventory)
    return index(inventory)


def test_missing_license_fails_but_writes_inventory_and_supplement_can_fix_it(inventory, tmp_path):
    package = Distribution(inventory.root, "no-license", {"bare.py": b"pass\n"}, version="2.0")
    inventory.distributions.append(package)
    inventory.analysis.pure.append(("bare", str(inventory.root / "bare.py"), "PYMODULE"))
    with pytest.raises(RuntimeError, match="no-license 2.0"):
        collect(inventory)
    assert index(inventory)["missing_license_texts"] == ["Python distribution no-license 2.0"]
    supplement = tmp_path / "supplements" / "no-license-2.0"
    supplement.mkdir(parents=True)
    (supplement / "provenance.json").write_text('{"commit":"fixed"}')
    with pytest.raises(RuntimeError, match="no-license"):
        collect(inventory, supplemental_dir=supplement.parent)
    (supplement / "LICENSE").write_bytes(b"upstream full text\n")
    collect(inventory, supplemental_dir=supplement.parent)
    assert index(inventory)["missing_license_texts"] == []
    restored = next(
        item for item in index(inventory)["distributions"] if item["name"] == "no-license"
    )
    assert len(restored["files"]) == 2


def test_native_full_notices_and_sqlite_header_disclaimer_are_preserved(inventory, tmp_path):
    prefix = tmp_path / "openssl-3.0"
    (prefix / "lib").mkdir(parents=True)
    library = prefix / "lib/libssl.3.dylib"
    library.write_bytes(b"binary")
    (prefix / "LICENSE.txt").write_bytes(b"complete OpenSSL license\n")
    sqlite = tmp_path / "sqlite-3.0"
    (sqlite / "lib").mkdir(parents=True)
    (sqlite / "include").mkdir()
    sqlite_library = sqlite / "lib/libsqlite3.dylib"
    sqlite_library.write_bytes(b"binary")
    comment = b"/*\n** The author disclaims copyright. Here is a blessing.\n"
    (sqlite / "include/sqlite3.h").write_bytes(
        comment
        + b"*************************************************************************\n** API docs */"
    )
    inventory.analysis.binaries = [
        ("libssl.3.dylib", str(library), "BINARY"),
        ("libsqlite3.dylib", str(sqlite_library), "BINARY"),
    ]
    collect(inventory)
    assert (
        inventory.output / "native/openssl/LICENSE.txt"
    ).read_bytes() == b"complete OpenSSL license\n"
    assert (
        inventory.output / "native/sqlite/sqlite3-header-notice.txt"
    ).read_bytes() == comment + b"*/\n"
    assert {item["name"] for item in index(inventory)["native_libraries"]} == {"openssl", "sqlite"}


def test_tcl_notice_collected_by_native_hook_is_reused(inventory, tmp_path, monkeypatch):
    prefix = tmp_path / "cpython"
    prefix.mkdir()
    library = prefix / "tcl86t.dll"
    library.write_bytes(b"native")
    license_file = prefix / "tcl8.6/license.terms"
    license_file.parent.mkdir()
    license_file.write_bytes(b"full Tcl terms\n")
    monkeypatch.setattr("scripts.collect_bundle_licenses.sys.base_prefix", str(prefix))
    inventory.analysis.binaries = [("tcl86t.dll", str(library), "BINARY")]
    inventory.analysis.datas = [("_tcl_data/license.terms", str(license_file), "DATA")]
    collect(inventory)
    assert index(inventory)["native_libraries"][0]["files"][0]["origin"] == "analyzed-data"


def test_plain_cpython_extension_does_not_create_false_native_license_gap(inventory):
    inventory.analysis.binaries = [
        (
            "zlib.cpython-313-darwin.so",
            str(inventory.root / "zlib.cpython-313-darwin.so"),
            "EXTENSION",
        )
    ]
    collect(inventory)
    assert index(inventory)["native_libraries"] == []


def test_rebuild_is_deterministic_and_does_not_return_stale_notice_files(inventory):
    first = collect(inventory)
    before = (inventory.output / "INDEX.json").read_bytes()
    (inventory.output / "obsolete-license").write_text("leftover from an old build")
    second = collect(inventory)
    assert first == second
    assert before == (inventory.output / "INDEX.json").read_bytes()
    assert not any("obsolete-license" in destination for destination, _source, _kind in second)


def test_supplement_external_symlink_is_not_copied(inventory, tmp_path):
    supplement = tmp_path / "supplements/example-1.2.3"
    supplement.mkdir(parents=True)
    (supplement / "LICENSE").symlink_to(inventory.python)
    with pytest.raises(ValueError, match="external link"):
        collect(inventory, supplemental_dir=supplement.parent)


def test_windows_python_dll_notices_require_and_use_portable_official_supplements(
    inventory, tmp_path, monkeypatch
):
    prefix = tmp_path / "windows-python"
    (prefix / "DLLs").mkdir(parents=True)
    (prefix / "LICENSE.txt").write_text("Only the CPython interpreter license")
    monkeypatch.setattr("scripts.collect_bundle_licenses.sys.base_prefix", str(prefix))
    names = [
        "libcrypto-3.dll",
        "libssl-3.dll",
        "libffi-8.dll",
        "sqlite3.dll",
        "tcl86t.dll",
        "tk86t.dll",
    ]
    for name in names:
        (prefix / "DLLs" / name).write_bytes(b"synthetic PE binary")
    inventory.analysis.binaries = [(name, str(prefix / "DLLs" / name), "BINARY") for name in names]
    with pytest.raises(RuntimeError, match="Native library openssl"):
        collect(inventory)
    # A generic Python LICENSE must not silently satisfy an SSL/FFI/Tcl notice.
    assert len(index(inventory)["missing_license_texts"]) == 5
    repository = Path(__file__).resolve().parent.parent
    collect(inventory, supplemental_dir=repository / "packaging/licenses")
    value = index(inventory)
    assert value["missing_license_texts"] == []
    assert {item["name"] for item in value["native_libraries"]} == {
        "openssl",
        "libffi",
        "sqlite",
        "tcl",
        "tk",
    }
    for component in value["native_libraries"]:
        assert component["files"]
        assert all(record["origin"] == "supplemental" for record in component["files"])
        assert any(record["path"].endswith("provenance.json") for record in component["files"])
        for record in component["files"]:
            payload = (inventory.output / record["path"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == record["sha256"]

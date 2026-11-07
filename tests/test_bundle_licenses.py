"""Build-only license collection preserves actual analyzed inputs, without network."""

from __future__ import annotations

import hashlib
import json
import tomllib
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
        "zlib1.dll",
    ]
    for name in names:
        (prefix / "DLLs" / name).write_bytes(b"synthetic PE binary")
    inventory.analysis.binaries = [(name, str(prefix / "DLLs" / name), "BINARY") for name in names]
    with pytest.raises(RuntimeError, match="Native library openssl"):
        collect(inventory)
    # A generic Python LICENSE must not silently satisfy a native dependency's notice.
    missing = index(inventory)["missing_license_texts"]
    assert len(missing) == 6
    assert "Native library zlib: zlib1.dll" in missing
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
        "zlib",
    }
    for component in value["native_libraries"]:
        assert component["files"]
        assert all(record["origin"] == "supplemental" for record in component["files"])
        assert any(record["path"].endswith("provenance.json") for record in component["files"])
        for record in component["files"]:
            payload = (inventory.output / record["path"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == record["sha256"]
            relative = Path(record["path"]).relative_to(
                Path("native") / component["name"] / "upstream"
            )
            source = repository / "packaging/licenses" / component["name"] / relative
            assert payload == source.read_bytes()


def test_zlib_supplement_preserves_both_upstream_copyright_notices_and_hashes():
    supplement = Path(__file__).resolve().parent.parent / "packaging/licenses/zlib"
    provenance = json.loads((supplement / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["version"] == "1.3.1"
    assert {record["path"] for record in provenance["files"]} == {"LICENSE", "README"}
    for record in provenance["files"]:
        payload = (supplement / record["path"]).read_bytes()
        assert len(payload) == record["bytes"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        git_blob = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
        assert hashlib.sha1(git_blob).hexdigest() == record["upstream_git_blob_sha1"]
    # Upstream's standalone LICENSE has an older year than its complete README notice.
    assert b"1995-2022 Jean-loup Gailly and Mark Adler" in (supplement / "LICENSE").read_bytes()
    assert b"1995-2024 Jean-loup Gailly and Mark Adler" in (supplement / "README").read_bytes()


RUST_SUPPLEMENTS = [
    (
        "tokenizers",
        "0.23.3",
        126,
        {"macos-arm64": "a65f99f1429fe5c517147b92427c5da5b86c040aa16236b3efb7f279477a8af2"},
    ),
    (
        "safetensors",
        "0.8.0",
        39,
        {"macos-arm64": "c5a46fbdca16e0075c5befd574475c483897ad60a6367e5bede9c5375ecfa543"},
    ),
    (
        "hf-xet",
        "1.8.0",
        258,
        {
            "macos-arm64": "be8904db442fd90b8fc2b250b20083f31ea0bacbbb3bd27fa7eb97aa5ee03770",
            "windows-amd64": "c5baa830d3d74900f7ec933fda48ebf5eebe35fa2657e7ab9860feddab7d392c",
        },
    ),
    (
        "pydantic_core",
        "2.50.2",
        101,
        {
            "macos-arm64": "22a4854711adf5f22542c9f07c6e9f5fd95954779dd0e01c18c46fb5dfde60e3",
            "windows-amd64": "3b8a1c35a92151ec75aeb6aa2fa8e9c2f438493eb28094d2d7c358b01954abb4",
        },
    ),
]


def verified_supplement_bytes(directory, record):
    relative = PurePosixPath(record["path"])
    assert not relative.is_absolute() and ".." not in relative.parts
    payload = directory.joinpath(*relative.parts).read_bytes()
    assert len(payload) == record["bytes"]
    assert hashlib.sha256(payload).hexdigest() == record["sha256"]
    return payload


@pytest.mark.parametrize("name,version,crate_count,sbom_hashes", RUST_SUPPLEMENTS)
def test_rust_notice_bytes_and_crate_sources_match_frozen_wheel_sboms(
    name, version, crate_count, sbom_hashes
):
    repository = Path(__file__).resolve().parent.parent
    supplement = repository / "packaging/licenses" / f"{name}-{version}"
    provenance = json.loads((supplement / "rust-provenance.json").read_bytes())
    assert (provenance["package"], provenance["version"]) == (name, version)
    locked = next(
        package
        for package in tomllib.loads((repository / "uv.lock").read_text())["package"]
        if package["name"] == name.replace("_", "-")
    )
    assert locked["version"] == version  # 依存の版を更新したら、必要な表示を再確認する。
    wheel_hashes = {wheel["url"]: wheel["hash"] for wheel in locked["wheels"]}
    expected, expected_sboms = {}, {}
    accounted = {"rust-provenance.json"}
    assert {wheel["platform"] for wheel in provenance["wheels"]} == set(sbom_hashes)
    for wheel in provenance["wheels"]:
        assert wheel_hashes[wheel["url"]] == f"sha256:{wheel['sha256']}"
        assert wheel["url"].endswith("/" + wheel["filename"])
        document = wheel["sbom"]
        assert document["sha256"] == sbom_hashes[wheel["platform"]]
        sbom = json.loads(verified_supplement_bytes(supplement, document))
        accounted.add(document["path"])
        for component in sbom["components"]:
            hashes = [h["content"] for h in component.get("hashes", []) if h["alg"] == "SHA-256"]
            if not hashes:
                assert component["bom-ref"].startswith("path+file:")
                continue  # ワークスペース内の crate は Python 配布物の既存ライセンスに従う。
            assert len(hashes) == 1
            identity = (component["name"], component["version"])
            assert expected.setdefault(identity, hashes[0]) == hashes[0]
            expected_sboms.setdefault(identity, set()).add(document["path"])
    crates = {(crate["name"], crate["version"]): crate for crate in provenance["crates"]}
    assert len(crates) == len(provenance["crates"]) == crate_count
    assert set(crates) == set(expected)
    for identity, crate in crates.items():
        crate_id = "-".join(identity)
        assert crate["archive_sha256"] == expected[identity]
        assert crate["archive_url"] == (
            f"https://static.crates.io/crates/{identity[0]}/{crate_id}.crate"
        )
        assert set(crate["sboms"]) == expected_sboms[identity]
        assert crate["files"]
        for document in crate["files"]:
            payload = verified_supplement_bytes(supplement, document)
            assert document["path"].startswith(f"rust/{crate_id}/")
            assert document["path"] not in accounted
            accounted.add(document["path"])
            if "archive_member" in document:
                assert document["archive_member"].startswith(crate_id + "/")
            elif "archive_excerpts" in document:
                assert document["archive_excerpts"]
                for source in document["archive_excerpts"]:
                    assert source["member"].startswith(crate_id + "/")
                    assert source["offset"] >= 0 and source["bytes"] == len(payload)
                    assert len(bytes.fromhex(source["member_sha256"])) == 32
            else:
                blob = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
                assert hashlib.sha1(blob).hexdigest() == document["upstream_git_blob_sha1"]
                fallback = crate["repository_fallback"]
                assert document["repository_commit"] == fallback["commit"]
                vcs = json.loads(
                    (
                        supplement / f"rust/{crate_id}/upstream-metadata/.cargo_vcs_info.json"
                    ).read_bytes()
                )
                assert vcs["git"]["sha1"] == fallback["commit"]
                assert document["source_url"].startswith(
                    fallback["repository"].replace("github.com", "raw.githubusercontent.com")
                    + "/"
                    + fallback["commit"]
                    + "/"
                )
    for document in provenance["reference_licenses"]:
        verified_supplement_bytes(supplement, document)
        assert document["applies_to"]
        assert all(tuple(identity.rsplit("-", 1)) in crates for identity in document["applies_to"])
        accounted.add(document["path"])
    if name == "tokenizers":
        accounted.update({"LICENSE", "provenance.json"})
    assert accounted == {
        path.relative_to(supplement).as_posix() for path in supplement.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("name,version,_count,_hashes", RUST_SUPPLEMENTS)
def test_frozen_rust_supplements_are_in_pyinstaller_toc_and_index_for_exact_selected_version(
    inventory, name, version, _count, _hashes
):
    repository = Path(__file__).resolve().parent.parent
    supplement = repository / "packaging/licenses" / f"{name}-{version}"
    # wheel 側に表示があっても、推移的な依存の補完文書を省略しない。
    package = Distribution(
        inventory.root,
        name,
        {f"{name}/native.so": b"synthetic native extension", f"{name}/LICENSE": b"wheel notice"},
        version=version,
    )
    inventory.distributions.append(package)
    inventory.analysis.binaries = [
        (f"{name}/native.so", str(inventory.root / name / "native.so"), "EXTENSION")
    ]
    records = collect(inventory, supplemental_dir=supplement.parent)
    bundled = next(item for item in index(inventory)["distributions"] if item["name"] == name)
    assert bundled["bundled_targets"] == [f"{name}/native.so"]
    prefix = f"python/{name}-{version}/upstream/"
    expected = {
        prefix + path.relative_to(supplement).as_posix(): path.read_bytes()
        for path in supplement.rglob("*")
        if path.is_file()
    }
    supplemental = {
        item["path"]: item for item in bundled["files"] if item["origin"] == "supplemental"
    }
    assert set(supplemental) == set(expected)
    toc = {target: (Path(source), kind) for target, source, kind in records}
    for relative, payload in expected.items():
        assert hashlib.sha256(payload).hexdigest() == supplemental[relative]["sha256"]
        source, kind = toc[f"third_party_licenses/{relative}"]
        assert kind == "DATA" and source.read_bytes() == payload
    package.version = version + ".post1"
    collect(inventory, supplemental_dir=supplement.parent)
    newer = next(item for item in index(inventory)["distributions"] if item["name"] == name)
    assert not any(item["origin"] == "supplemental" for item in newer["files"])
    inventory.analysis.binaries = []
    assert not any(
        name in target
        for target, _source, _kind in collect(inventory, supplemental_dir=supplement.parent)
    )


@pytest.mark.parametrize(
    "relative,sha256,copyright_holder",
    [
        (
            "rust/onig-6.5.3/LICENSE.md",
            "e9c1238c5beb73c6cc130160418968058b21d486a7a445f0ceff27d51fbfdc44",
            b"2015 Will Speak",
        ),
        (
            "rust/onig_sys-69.9.3/LICENSE.md",
            "71f321038b088358004bee991635ac09e4c703bec467d3c30c06992c0595f189",
            b"Ivan Ivashchenko",
        ),
        (
            "rust/onig_sys-69.9.3/oniguruma/COPYING",
            "70ba5469ea0bab6e18a32d7009068f996503168d27be57747e08da34337ff26f",
            b"2002-2021  K.Kosako",
        ),
        (
            "rust/onig_sys-69.9.3/source-notices/cp1251.c-8bfc7882a4088068.txt",
            "8bfc7882a408806822c231859987baf1792d8318811ee487f3c6043ff6387eb9",
            b"2006-2020  Byte",
        ),
        (
            "rust/onig_sys-69.9.3/source-notices/gb18030.c-dd36a2e6a824afb9.txt",
            "dd36a2e6a824afb91674897fecd3e12a32b0611948e57814b111214fe3dfa191",
            b"KUBO Takehiro",
        ),
        (
            "rust/esaxx-rs-0.1.10/source-notices/esaxx.cpp-c09f3834a1f86881.txt",
            "c09f3834a1f86881258ba19056861ec716446c755f69616423b1af1257d49f35",
            b"Yuta Mori",
        ),
        (
            "rust/esaxx-rs-0.1.10/source-notices/esaxx.cpp-8aac061b3fa2f67a.txt",
            "8aac061b3fa2f67ac89b68e26eb02bc76a35c33b6cce73e9aa764f2b9df0ab9d",
            b"Daisuke Okanohara",
        ),
    ],
)
def test_rust_onig_and_esaxx_individual_notice_texts_are_pinned(relative, sha256, copyright_holder):
    supplement = Path(__file__).resolve().parent.parent / "packaging/licenses/tokenizers-0.23.3"
    payload = (supplement / relative).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == sha256
    assert copyright_holder in payload
    assert b"SOFTWARE" in payload and b"LIAB" in payload

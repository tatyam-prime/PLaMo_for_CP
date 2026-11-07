from __future__ import annotations

import plistlib
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from plamo_cp import desktop, windows_app
from plamo_cp.launch_protocol import OPEN_URL, launch_mode
from scripts.build_macos_app import bundle_info

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "arguments,mode",
    [
        ([], "app"),
        (["--open-url", OPEN_URL], "open"),
        (["--server-child"], "server"),
        (["--native-worker"], "native"),
        (["--server-child", "--port", "8767"], "server"),
    ],
)
def test_only_explicit_desktop_modes_are_accepted(arguments, mode):
    assert launch_mode(arguments) == mode


@pytest.mark.parametrize(
    "arguments",
    [
        [OPEN_URL],
        ["--open-url"],
        ["--open-url", OPEN_URL, "--server-child"],
        ["--server-child", "--open-url", OPEN_URL],
        ["--open-url", 'plamo-for-cp://open" --server-child --port 8888'],
        ["--open-url", "plamo-for-cp://open?model=8b"],
        ["--open-url", "plamo-for-cp://open/path"],
        ["--open-url", "file:///etc/passwd"],
        ["--open-url", "https://example.org/"],
        ["--server-child", "--port", "0"],
        ["--server-child", "--port", "65536"],
        ["--server-child", "--port", "８７６５"],
        ["--server-child", "--port", "8765", "--server-child"],
        ["--native-worker", "--port", "8765"],
        ["--native-worker", "--open-url", OPEN_URL],
        ["--open-url", OPEN_URL, "--native-worker"],
        ["--native-worker", "--server-child"],
        ["--multiprocessing-fork", "parent_pid=123", "pipe_handle=456"],
    ],
)
def test_invalid_protocol_arguments_cannot_reach_an_application_entrypoint(monkeypatch, arguments):
    entry = runpy.run_path(str(ROOT / "packaging/windows/entry.py"))
    calls = []
    monkeypatch.setattr(sys, "argv", ["PLaMo for 競プロ.exe", *arguments])
    monkeypatch.setattr(entry["multiprocessing"], "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(desktop, "main", lambda: calls.append("server"))
    monkeypatch.setattr(windows_app, "main", lambda **_: calls.append("app"))
    assert entry["main"]() == 2
    assert calls == ["freeze"]
    with pytest.raises(ValueError):
        launch_mode(arguments)


def test_standard_frozen_worker_handoff_precedes_application_dispatch(monkeypatch):
    entry = runpy.run_path(str(ROOT / "packaging/windows/entry.py"))
    monkeypatch.setattr(sys, "argv", ["PLaMo for 競プロ.exe", "--multiprocessing-fork"])

    def worker_handoff():
        raise SystemExit(17)

    monkeypatch.setattr(entry["multiprocessing"], "freeze_support", worker_handoff)
    monkeypatch.setattr(desktop, "main", lambda: pytest.fail("not an app server launch"))
    monkeypatch.setattr(windows_app, "main", lambda **_: pytest.fail("not a GUI launch"))
    with pytest.raises(SystemExit) as result:
        entry["main"]()
    assert result.value.code == 17


@pytest.mark.parametrize("arguments,protocol", [([], False), (["--open-url", OPEN_URL], True)])
def test_external_open_dispatches_only_to_the_native_launcher(monkeypatch, arguments, protocol):
    entry = runpy.run_path(str(ROOT / "packaging/windows/entry.py"))
    calls = []
    monkeypatch.setattr(sys, "argv", ["PLaMo for 競プロ.exe", *arguments])
    monkeypatch.setattr(entry["multiprocessing"], "freeze_support", lambda: None)
    monkeypatch.setattr(desktop, "main", lambda: pytest.fail("must not enter server mode"))
    monkeypatch.setattr(windows_app, "main", lambda **kwargs: calls.append(kwargs))
    assert entry["main"]() == 0
    assert calls == [{"protocol_launch": protocol}]


def test_private_backend_port_is_the_only_argument_forwarded(monkeypatch):
    entry = runpy.run_path(str(ROOT / "packaging/windows/entry.py"))
    monkeypatch.setattr(sys, "argv", ["PLaMo for 競プロ.exe", "--server-child", "--port", "8767"])
    monkeypatch.setattr(entry["multiprocessing"], "freeze_support", lambda: None)
    calls = []
    monkeypatch.setattr(desktop, "main", lambda: calls.append(list(sys.argv)) or 3)
    monkeypatch.setattr(windows_app, "main", lambda **_: pytest.fail("must not open a window"))
    assert entry["main"]() == 3
    assert calls == [["PLaMo for 競プロ.exe", "--port", "8767"]]


def test_native_worker_dispatch_is_isolated_and_has_no_external_arguments(monkeypatch):
    entry = runpy.run_path(str(ROOT / "packaging/windows/entry.py"))
    monkeypatch.setattr(sys, "argv", ["PLaMo for 競プロ.exe", "--native-worker"])
    calls = []
    monkeypatch.setattr(entry["multiprocessing"], "freeze_support", lambda: calls.append("freeze"))
    worker = ModuleType("plamo_cp.native_worker")

    def main():
        calls.append(list(sys.argv))
        return 37

    worker.main = main
    monkeypatch.setitem(sys.modules, "plamo_cp.native_worker", worker)
    monkeypatch.setattr(desktop, "main", lambda: pytest.fail("not a server launch"))
    monkeypatch.setattr(windows_app, "main", lambda **_: pytest.fail("not a GUI launch"))
    assert entry["main"]() == 37
    assert calls == ["freeze", ["PLaMo for 競プロ.exe"]]


def test_installer_registers_a_user_only_constant_command_without_uri_substitution():
    installer = (ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8-sig")
    registry = installer.split("[Registry]", 1)[1].split("[Run]", 1)[0]
    entries = [line for line in registry.splitlines() if line.startswith("Root:")]
    assert entries and all(line.startswith("Root: HKCU;") for line in entries)
    command = next(line for line in entries if r"\shell\open\command" in line)
    # The actual registered command contains no %1, so Windows cannot turn a
    # crafted URI with quotes/backslashes into extra private launch arguments.
    assert "%" not in command
    assert (
        'ValueData: """{app}\\PLaMo for 競プロ.exe"" --open-url ""plamo-for-cp://open"""' in command
    )
    assert any('ValueName: "URL Protocol"' in line for line in entries)
    assert "uninsdeletekey" in entries[0]


def test_macos_info_registers_only_the_application_scheme():
    info = plistlib.loads(plistlib.dumps(bundle_info("27.0")))
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "PLaMo for 競プロ"
    # An ASCII executable avoids codesign NFC/NFD self-sealing on Japanese names.
    assert info["CFBundleExecutable"].isascii()
    assert "/" not in info["CFBundleExecutable"]
    assert info["CFBundleURLTypes"] == [
        {
            "CFBundleURLName": "jp.plamo-for-cp.desktop.open",
            "CFBundleURLSchemes": ["plamo-for-cp"],
            "CFBundleTypeRole": "Viewer",
        }
    ]
    assert info["CFBundleIdentifier"] == "jp.plamo-for-cp.desktop"


@pytest.fixture(scope="module")
def swift_url_validator(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Native URL grammar requires macOS and Swift")
    directory = tmp_path_factory.mktemp("native-launch-protocol")
    main = directory / "LaunchMain.swift"
    main.write_text(
        """import Foundation
@main struct Main {
    static func main() {
        let strings = Array(CommandLine.arguments.dropFirst())
        let urls = strings.compactMap { URL(string: $0) }
        print(urls.count == strings.count && AppLaunchProtocol.accepts(urls))
    }
}
"""
    )
    binary = directory / "validate-url"
    subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "5",
            str(ROOT / "packaging/macos/AppLaunch.swift"),
            str(main),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
    )

    def validate(arguments):
        return subprocess.check_output([str(binary), *arguments], text=True, timeout=3).strip()

    return validate


@pytest.mark.parametrize(
    "urls,accepted",
    [
        ([OPEN_URL], "true"),
        ([], "false"),
        ([OPEN_URL, OPEN_URL], "false"),
        (["plamo-for-cp://open/"], "false"),
        (["plamo-for-cp://open?model=8b"], "false"),
        (["plamo-for-cp://open#text"], "false"),
        (["plamo-for-cp://user@open"], "false"),
        (["plamo-for-cp://open:8765"], "false"),
        (["plamo-for-cp://%6fpen"], "false"),
        (["plamo-for-cp://--server-child"], "false"),
        (["file:///etc/passwd"], "false"),
        (["https://example.org/"], "false"),
    ],
)
def test_native_url_action_accepts_only_the_single_fixed_target(
    swift_url_validator, urls, accepted
):
    assert swift_url_validator(urls) == accepted

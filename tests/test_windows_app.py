from __future__ import annotations

import sys
from types import SimpleNamespace

from plamo_cp import windows_app


def test_backend_command_for_frozen_application(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Apps/PLaMo/PLaMo for CP.exe")
    assert windows_app.backend_command() == ["C:/Apps/PLaMo/PLaMo for CP.exe", "--server-child"]


def test_backend_command_for_development(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "python")
    assert windows_app.backend_command() == ["python", "-m", "plamo_cp.desktop"]


def test_shutdown_never_stops_an_external_service(monkeypatch):
    monkeypatch.setattr(
        windows_app.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    windows_app.stop_owned_process(None)
    windows_app.stop_owned_process(SimpleNamespace(poll=lambda: 0))


def test_windows_shutdown_targets_only_owned_process_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(windows_app.sys, "platform", "win32")
    monkeypatch.setattr(windows_app.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        windows_app.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )
    windows_app.stop_owned_process(SimpleNamespace(pid=12345, poll=lambda: None))
    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert calls[0][1]["creationflags"] == 0x08000000

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from plamo_cp import folder_picker


def test_macos_paths_are_argv_data_and_have_no_short_timeout(monkeypatch, tmp_path):
    initial = tmp_path / 'モデル "quoted" $(not-a-command)'
    # Test argv data without requiring the host filesystem to allow macOS filenames.
    monkeypatch.setattr(folder_picker, "_existing_parent", lambda path: path)
    selected = tmp_path / "選んだフォルダー"
    calls = []
    monkeypatch.setattr(folder_picker.platform, "system", lambda: "Darwin")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(selected) + "/\n", stderr="")

    monkeypatch.setattr(folder_picker.subprocess, "run", run)
    assert folder_picker.choose_folder(initial) == selected
    args, options = calls[0]
    assert args[-1] == str(initial)
    assert str(initial) not in args[2]
    assert "timeout" not in options
    assert options["encoding"] == "utf-8"


@pytest.mark.parametrize("code,stdout,stderr", [(0, "\n", ""), (1, "", "User canceled. (-128)")])
def test_macos_cancellation_is_not_an_error(monkeypatch, tmp_path, code, stdout, stderr):
    monkeypatch.setattr(folder_picker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        folder_picker.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr),
    )
    assert folder_picker.choose_folder(tmp_path) is None


def test_windows_initial_path_is_environment_data_not_powershell_source(monkeypatch, tmp_path):
    initial = tmp_path / "quoted';$(command)"
    initial.mkdir()
    selected = tmp_path / "保存先"
    calls = []
    monkeypatch.setattr(folder_picker.platform, "system", lambda: "Windows")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"cancelled": False, "folder": str(selected)}, ensure_ascii=False),
            stderr="",
        )

    monkeypatch.setattr(folder_picker.subprocess, "run", run)
    assert folder_picker.choose_folder(initial) == selected
    args, options = calls[0]
    script = base64.b64decode(args[-1]).decode("utf-16-le")
    assert str(initial) not in script
    assert options["env"]["PLAMO_FOLDER_PICKER_INITIAL"] == str(initial)
    assert "-STA" in args and "-EncodedCommand" in args
    assert "timeout" not in options


def test_windows_cancel_and_missing_picker(monkeypatch, tmp_path):
    monkeypatch.setattr(folder_picker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        folder_picker.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout='{"cancelled":true}', stderr=""
        ),
    )
    assert folder_picker.choose_folder(tmp_path) is None

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(folder_picker.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="フォルダー選択画面"):
        folder_picker.choose_folder(tmp_path)


def test_nonexistent_initial_path_uses_existing_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(folder_picker.platform, "system", lambda: "Darwin")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(folder_picker.subprocess, "run", run)
    assert folder_picker.choose_folder(tmp_path / "not-created/models") is None
    assert calls[0][-1] == str(tmp_path)

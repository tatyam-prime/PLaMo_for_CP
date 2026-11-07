"""Native folder selection invoked only by an explicit management action."""

from __future__ import annotations

import base64
import json
import os
import platform
import subprocess
from pathlib import Path

MACOS_SCRIPT = """on run argv
    try
        set selectedFolder to choose folder with prompt "PLaMo for 競プロ：モデルの保存先を選択" default location (POSIX file (item 1 of argv))
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run"""

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'PLaMo for 競プロ：モデルの保存先を選択'
$dialog.SelectedPath = [Environment]::GetEnvironmentVariable('PLAMO_FOLDER_PICKER_INITIAL')
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        @{cancelled=$false; folder=$dialog.SelectedPath} | ConvertTo-Json -Compress
    } else {
        @{cancelled=$true} | ConvertTo-Json -Compress
    }
} finally {
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}
"""


def _existing_parent(initial: Path) -> Path:
    path = initial.expanduser().absolute()
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path if path.is_dir() else Path.home()


def choose_folder(initial: Path) -> Path | None:
    """Return None for normal cancellation; do not put paths into program text."""
    directory = _existing_parent(initial)
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", MACOS_SCRIPT, str(directory)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if result.returncode:
                if "(-128)" in result.stderr:
                    return None
                raise RuntimeError("フォルダー選択画面を開けませんでした。もう一度お試しください。")
            value = result.stdout.rstrip("\r\n")
            return Path(value) if value else None
        if system == "Windows":
            executable = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            environment = dict(os.environ)
            environment["PLAMO_FOLDER_PICKER_INITIAL"] = str(directory)
            encoded = base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(
                [
                    str(executable),
                    "-NoProfile",
                    "-STA",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-EncodedCommand",
                    encoded,
                ],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                raise RuntimeError("フォルダー選択画面を開けませんでした。もう一度お試しください。")
            value = json.loads(result.stdout.lstrip("\ufeff"))
            if not isinstance(value, dict):
                raise RuntimeError(
                    "選択したフォルダーを確認できませんでした。もう一度お試しください。"
                )
            if value.get("cancelled") is True:
                return None
            folder = value.get("folder")
            if isinstance(folder, str) and folder:
                return Path(folder)
            raise RuntimeError("選択したフォルダーを確認できませんでした。もう一度お試しください。")
    except (OSError, ValueError, UnicodeError) as error:
        raise RuntimeError(
            "フォルダー選択画面を開けませんでした。アプリを開き直してお試しください。"
        ) from error
    raise RuntimeError("この環境ではフォルダー選択を利用できません。")

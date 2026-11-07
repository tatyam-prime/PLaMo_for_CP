"""Verify the bundled native runtime against pins embedded in frozen Python code.

This module never loads a DLL, starts a child, downloads a file, or trusts a
sidecar JSON manifest. The native worker independently repeats this check before
loading any library. Missing development pins mean the CPU server is used.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import _native_runtime_pins

NATIVE_RELEASE = "b12119"
BRIDGE_NAME = "cp_llama_bridge.dll"
BRIDGE_ABI = 1
MAX_RUNTIME_FILES = 4096
MAX_RUNTIME_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class NativeRuntime:
    directory: Path
    bridge: Path
    architecture: str


def _relative_name(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise RuntimeError("ネイティブ実行環境の固定パスが不正です。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ""} for part in value.split("/")):
        raise RuntimeError("ネイティブ実行環境の固定パスが不正です。")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
    }
    if any(part[-1] in ". " or part.split(".", 1)[0].upper() in reserved for part in path.parts):
        raise RuntimeError("ネイティブ実行環境の固定パスが不正です。")
    return path


def _regular_tree(directory: Path) -> dict[str, Path]:
    if not directory.is_absolute():
        raise RuntimeError("ネイティブ実行環境には絶対パスが必要です。")
    for ancestor in directory.parents:
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("ネイティブ実行環境の保存先にリンクは使用できません。")
    result, pending, names = {}, [directory], set()
    total = 0
    while pending:
        path = pending.pop()
        info = path.lstat()
        # FILE_ATTRIBUTE_REPARSE_POINT also catches Windows junctions. Do not
        # recurse through links before discovering that a path escapes the root.
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("ネイティブ実行環境にリンクは使用できません。")
        relative = path.relative_to(directory).as_posix()
        if path != directory:
            _relative_name(relative)
            identity = relative.casefold()
            if identity in names:
                raise RuntimeError("ネイティブ実行環境に重複したパスがあります。")
            names.add(identity)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    if len(names) + len(pending) >= MAX_RUNTIME_FILES:
                        raise RuntimeError("ネイティブ実行環境のファイル数が多すぎます。")
                    pending.append(Path(entry.path))
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            result[relative] = path
        else:
            raise RuntimeError("ネイティブ実行環境に通常ファイル以外が含まれています。")
        if len(names) > MAX_RUNTIME_FILES or total > MAX_RUNTIME_BYTES:
            raise RuntimeError("ネイティブ実行環境のサイズが大きすぎます。")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_file_pins(directory: Path) -> dict[str, dict]:
    """Build-time inventory; callers must first verify the official archive."""
    return {
        name: {"sha256": _sha256(path), "size": path.stat().st_size}
        for name, path in sorted(_regular_tree(directory).items())
    }


def verify_native_runtime_directory(directory: Path, pins: dict) -> NativeRuntime:
    """Validate an explicit build/test directory without loading any library."""
    try:
        if (
            not isinstance(pins, dict)
            or type(pins.get("schema")) is not int
            or pins.get("schema") != 1
            or pins.get("llama_release") != NATIVE_RELEASE
            or type(pins.get("bridge_abi")) is not int
            or pins.get("bridge_abi") != BRIDGE_ABI
            or pins.get("architecture") not in {"x64", "arm64"}
        ):
            raise RuntimeError("ネイティブ実行環境の固定情報が不正です。")
        files = pins.get("files")
        if not isinstance(files, dict) or not 0 < len(files) <= MAX_RUNTIME_FILES:
            raise RuntimeError("ネイティブ実行環境の固定ファイル一覧が不正です。")
        normalized = set()
        for name, information in files.items():
            relative = _relative_name(name)
            if name.casefold() in normalized:
                raise RuntimeError("ネイティブ実行環境の固定パスが重複しています。")
            normalized.add(name.casefold())
            if (
                not isinstance(information, dict)
                or type(information.get("size")) is not int
                or not 0 <= information["size"] <= MAX_RUNTIME_BYTES
                or not isinstance(information.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", information["sha256"])
                or (relative.suffix.casefold() == ".dll" and len(relative.parts) != 1)
            ):
                raise RuntimeError("ネイティブ実行環境の固定ファイル情報が不正です。")
        required = {BRIDGE_NAME, "llama.dll", "ggml.dll", "ggml-base.dll", "ggml-vulkan.dll"}
        if not required <= set(files) or not any(
            name.startswith("ggml-cpu") and name.endswith(".dll") for name in files
        ):
            raise RuntimeError("ネイティブ実行環境に必要な DLL がありません。")
        actual = _regular_tree(directory)
        if set(actual) != set(files):
            raise RuntimeError("ネイティブ実行環境に未登録・欠落したファイルがあります。")
        for name, path in actual.items():
            information = files[name]
            if path.stat().st_size != information["size"] or _sha256(path) != information["sha256"]:
                raise RuntimeError("ネイティブ実行環境のファイル検証に失敗しました。")
        return NativeRuntime(directory, directory / BRIDGE_NAME, pins["architecture"])
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError("ネイティブ実行環境を確認できませんでした。") from error


def verified_native_runtime() -> NativeRuntime | None:
    pins = _native_runtime_pins.RUNTIME_PINS
    if pins is None:
        return None
    if not getattr(sys, "frozen", False):
        raise RuntimeError("ネイティブ実行環境は検証済みの配布アプリで使用してください。")
    base = Path(getattr(sys, "_MEIPASS", ""))
    if not base.is_absolute():
        raise RuntimeError("同梱ネイティブ実行環境の保存先が不正です。")
    runtime = verify_native_runtime_directory(base / "native", pins)
    from .llama_backend import runtime_arch

    if runtime.architecture != runtime_arch():
        raise RuntimeError("ネイティブ実行環境がこの Windows の種類に対応していません。")
    return runtime

"""Windows CPU inference with pinned official GGUF weights and llama.cpp.

No process or download starts at construction time. ``prepare`` is the explicit
setup action; translations only reuse previously prepared local files.
"""

from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import httpx

from .engine import (
    DEFAULT_MODEL_ID,
    SUPPORTED_MODEL_IDS,
    check_placeholders,
    translation_options,
    validate_history,
)

log = logging.getLogger(__name__)
Progress = Callable[[dict[str, Any]], None] | None
CONTEXT_SIZE = 32768
MAX_RUNTIME_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_FILES = 4096
LLAMA_RELEASE = "b12119"
RUNTIME_ASSETS = {
    "x64": {
        "filename": "llama-b12119-bin-win-cpu-x64.zip",
        "size": 24730637,
        "sha256": "37a6c6c9eae5b7baa2619cba513781fad15ca0f63f7d07362ec83a294cf96f86",
    },
    "arm64": {
        "filename": "llama-b12119-bin-win-cpu-arm64.zip",
        "size": 13225317,
        "sha256": "c06d7199371b3d9fe8c49ef7a1cb7c9559ccf927c4e6390108cc5bd8a8c81832",
    },
}
GGUF_MODELS: dict[str, dict[str, Any]] = {
    "0.8b": {
        "revision": "03891e907db8a43c97d3a7c75add125cc6d1f96a",
        "weight_bytes": 573116288,
        "sha256": "3dbb09295bfd3350231ba0dba92bc72cb14111ef1b034e37dad39d581bd86b9c",
        "min_memory_gb": 4,
    },
    "2b": {
        "revision": "f2b4599dff9b3e45c62567eac15e83186b4cb64e",
        "weight_bytes": 1807192480,
        "sha256": "2c288ecc7ebfe60c3561726580da3583767f3c41ca3ef5c6825bf6c4cc060787",
        "min_memory_gb": 8,
    },
    "8b": {
        "revision": "c420296e9694ccb2455f038b3f29eee5a2200217",
        "weight_bytes": 5280435168,
        "sha256": "6b429549113ce0a61281ac72be195697241f89266f4247efec4893e4f580cf10",
        "min_memory_gb": 16,
    },
    "31b": {
        "revision": "d0df961b923db3ff9fc325f4410d6f09005afcd6",
        "weight_bytes": 19526583392,
        "sha256": "d1ed08149f888ef7a0da38e26bf394a13d0f079b592229c8778f5f1a91cf6457",
        "min_memory_gb": 32,
    },
}
for _size, _spec in GGUF_MODELS.items():
    _spec.update(
        name=f"PLaMo 3 Translate {_size.upper()}",
        repo=f"pfnet/plamo-3-translate-{_size}-GGUF",
        filename=f"plamo-3-translate-{_size}-Q4_K_M.gguf",
        weight_gb=round(_spec["weight_bytes"] / 1e9, 2),
        precision="Q4_K_M",
    )


def _notify(progress: Progress, **event: Any) -> None:
    if progress is not None:
        progress(event)


def _sha256(path: Path, *, cancelled=None) -> str:
    from .download import check_download_cancelled

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            check_download_cancelled(cancelled)
            digest.update(chunk)
    return digest.hexdigest()


def runtime_arch(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise RuntimeError("Windows 64ビット版（x64 / ARM64）が必要です。")


def _runtime_home() -> Path:
    return Path.home() / ".cache" / "plamo-cp" / "llama" / LLAMA_RELEASE / runtime_arch()


def _runtime_candidates() -> list[Path]:
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "llama"
    return [bundle, _runtime_home()]


def _runtime_name(value: str) -> PurePosixPath:
    """Accept portable relative paths without Windows aliases or device names."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Invalid runtime path")
    parts = value.replace("\\", "/").rstrip("/").split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
    }
    if any(
        not part
        or part in {".", ".."}
        or part[-1] in ". "
        or ":" in part
        or any(ord(character) < 32 for character in part)
        or part.split(".", 1)[0].upper() in reserved
        for part in parts
    ):
        raise ValueError("Invalid runtime path")
    return PurePosixPath(*parts)


def _runtime_path(directory: Path, name: str) -> Path:
    relative = _runtime_name(name)
    root = directory.resolve()
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Runtime path escapes its directory")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Runtime files must not be links")
    return path


def _runtime_archive(directory: Path) -> Path | None:
    """The pinned ZIP is the trust anchor; a writable manifest cannot assert trust."""
    asset = RUNTIME_ASSETS[runtime_arch()]
    for path in (directory / asset["filename"], directory.parent / asset["filename"]):
        if path.is_file() and path.stat().st_size == asset["size"]:
            return path
    return None


def _runtime_members(source: zipfile.ZipFile):
    members, names, total = [], set(), 0
    for member in source.infolist():
        try:
            name = _runtime_name(member.orig_filename)
        except ValueError as error:
            raise RuntimeError("実行環境アーカイブに不正なパスが含まれています。") from error
        normalized = name.as_posix().casefold()
        if normalized in names:
            raise RuntimeError("実行環境アーカイブに重複したパスが含まれています。")
        names.add(normalized)
        mode = (member.external_attr >> 16) & 0o170000
        if mode not in {0, 0o040000, 0o100000}:
            raise RuntimeError("実行環境アーカイブのリンクは展開できません。")
        total += member.file_size
        if total > MAX_RUNTIME_BYTES or len(names) > MAX_RUNTIME_FILES:
            raise RuntimeError("実行環境アーカイブのサイズが大きすぎます。")
        if not member.is_dir():
            members.append((name, member))
    return members


def _archive_runtime_files(archive: Path) -> dict[str, dict[str, Any]]:
    result = {}
    with zipfile.ZipFile(archive) as source:
        for name, member in _runtime_members(source):
            digest = hashlib.sha256()
            with source.open(member) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[name.as_posix()] = {"size": member.file_size, "sha256": digest.hexdigest()}
    return result


def _runtime_executable(directory: Path, *, verify: bool = False) -> Path | None:
    try:
        directory = directory.resolve()
        manifest = json.loads((directory / ".runtime-manifest.json").read_text())
        asset = RUNTIME_ASSETS[runtime_arch()]
        if manifest["archive_sha256"] != asset["sha256"]:
            return None
        files = manifest["files"]
        if not isinstance(files, dict) or not 0 < len(files) <= MAX_RUNTIME_FILES:
            return None
        executable_name = _runtime_name(manifest["executable"]).as_posix()
        if (
            executable_name not in files
            or PurePosixPath(executable_name).name != "llama-server.exe"
        ):
            return None
        archive = _runtime_archive(directory)
        if archive is None:
            return None
        if verify and (
            _sha256(archive) != asset["sha256"] or _archive_runtime_files(archive) != files
        ):
            return None
        for name, info in files.items():
            path = _runtime_path(directory, name)
            if not path.is_file() or path.stat().st_size != info["size"]:
                return None
            if verify and _sha256(path) != info["sha256"]:
                return None
        allowed = {directory / name for name in files} | {
            directory / ".runtime-manifest.json",
            directory / asset["filename"],
        }
        # Reject injected DLLs and directory links, not only changes to listed files.
        if any(
            path.is_symlink() or (path.is_file() and path not in allowed)
            for path in directory.rglob("*")
        ):
            return None
        return _runtime_path(directory, executable_name)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile):
        return None


def _safe_extract(archive: Path, destination: Path) -> dict[str, dict[str, Any]]:
    with zipfile.ZipFile(archive) as source:
        files = {}
        for name, member in _runtime_members(source):
            target = _runtime_path(destination, name.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            files[name.as_posix()] = {"size": target.stat().st_size, "sha256": _sha256(target)}
        return files


def ensure_runtime(
    progress: Progress = None, *, directory: Path | None = None, cancelled=None
) -> Path:
    """Install a hash-checked portable CPU runtime, only from explicit setup."""
    from .download import (
        BandwidthLimiter,
        _download_lock,
        check_download_cancelled,
        limited_client_factory,
        resolve_rate,
    )

    check_download_cancelled(cancelled)
    target = directory or _runtime_home()
    if directory is None:
        for candidate in _runtime_candidates():
            if executable := _runtime_executable(candidate, verify=True):
                return executable
    asset = RUNTIME_ASSETS[runtime_arch()]
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.parent / asset["filename"]
    with _download_lock(cancelled):
        if executable := _runtime_executable(target, verify=True):
            return executable
        if not archive.is_file() or _sha256(archive) != asset["sha256"]:
            rate = resolve_rate()
            client = limited_client_factory(BandwidthLimiter(rate, cancelled=cancelled))
            client.timeout = httpx.Timeout(10, connect=5)
            url = (
                f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/"
                + asset["filename"]
            )
            partial = archive.with_suffix(".part")
            completed = 0
            _notify(
                progress,
                stage="downloading_runtime",
                downloaded_bytes=0,
                total_bytes=asset["size"],
                message="翻訳エンジンを準備しています。",
            )
            with client, client.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes(64 * 1024):
                        completed += len(chunk)
                        if completed > asset["size"]:
                            raise RuntimeError("翻訳エンジンの取得サイズが一致しません。")
                        output.write(chunk)
                        _notify(
                            progress,
                            stage="downloading_runtime",
                            downloaded_bytes=completed,
                            total_bytes=asset["size"],
                        )
            if completed != asset["size"] or _sha256(partial) != asset["sha256"]:
                partial.unlink(missing_ok=True)
                raise RuntimeError(
                    "翻訳エンジンのファイル検証に失敗しました。もう一度準備してください。"
                )
            partial.replace(archive)
        check_download_cancelled(cancelled)
        _notify(progress, stage="verifying_runtime", message="翻訳エンジンを確認しています。")
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            extracted = Path(temporary) / "runtime"
            extracted.mkdir()
            files = _safe_extract(archive, extracted)
            candidates = list(extracted.rglob("llama-server.exe"))
            if len(candidates) != 1:
                raise RuntimeError("翻訳エンジンがアーカイブに見つかりません。")
            manifest = {
                "archive_sha256": asset["sha256"],
                "files": files,
                "executable": candidates[0].relative_to(extracted).as_posix(),
            }
            (extracted / ".runtime-manifest.json").write_text(json.dumps(manifest))
            if target.exists():
                shutil.rmtree(target)
            extracted.replace(target)
        return target / manifest["executable"]


def build_prompt(
    text: str,
    source_lang: str,
    target_lang: str,
    document_type: str = "problem",
    history: list[dict] | None = None,
    glossary: list[dict] | None = None,
    source_context: str = "",
) -> str:
    options = translation_options(
        text,
        source_lang,
        target_lang,
        document_type=document_type,
        glossary=glossary,
        source_context=source_context,
    )
    prompt = "<|plamo:bos|><|plamo:tag|>dataset<|plamo:msg|>translation"
    prompt += "<|plamo:tag|>context<|plamo:msg|>" + options["context"]
    for pair in validate_history(history):
        prompt += "<|plamo:tag|>input"
        if options["source_lang"]:
            prompt += "<|plamo:key|>lang<|plamo:val|>" + options["source_lang"]
        prompt += "<|plamo:msg|>" + pair["source"]
        prompt += "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>" + options["target_lang"]
        prompt += "<|plamo:msg|>" + pair["translation"]
    if options["glossary"]:
        prompt += "<|plamo:tag|>glossary"
        for term in options["glossary"]:
            prompt += "<|plamo:index|>" + term["source"] + "<|plamo:item|>" + term["target"]
    prompt += "<|plamo:tag|>input"
    if options["source_lang"]:
        prompt += "<|plamo:key|>lang<|plamo:val|>" + options["source_lang"]
    prompt += "<|plamo:msg|>" + text
    prompt += "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>" + options["target_lang"]
    if "form" in options:
        prompt += "<|plamo:key|>form<|plamo:val|>" + options["form"]
    return prompt + "<|plamo:msg|>"


def windows_memory_gb() -> float:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong)
            for name in (
                "total_physical",
                "available_physical",
                "total_page",
                "available_page",
                "total_virtual",
                "available_virtual",
                "extended_virtual",
            )
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise RuntimeError("メモリ容量を確認できませんでした。")
    return status.total_physical / 1024**3


class LlamaEngine:
    model_specs: ClassVar[dict] = {model: GGUF_MODELS[model] for model in SUPPORTED_MODEL_IDS}
    storage_model_specs = GGUF_MODELS
    supported_batch_sizes = (1,)

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        from .settings import model_cache_dir

        self.cache_dir = model_cache_dir(cache_dir)
        self._lock = threading.RLock()
        self._process = None
        self._client: httpx.Client | None = None
        self._loaded: str | None = None
        self._state = "idle"
        self._log_thread = None
        atexit.register(self.close)

    @property
    def status(self) -> dict[str, Any]:
        return {"backend": "llama.cpp", "loaded_model": self._loaded, "state": self._state}

    def _model_path(self, model: str) -> Path | None:
        from huggingface_hub import snapshot_download

        spec = self.model_specs[model]
        try:
            directory = snapshot_download(
                spec["repo"],
                revision=spec["revision"],
                local_files_only=True,
                cache_dir=str(self.cache_dir),
            )
            path = Path(directory) / spec["filename"]
            return path if path.is_file() and path.stat().st_size == spec["weight_bytes"] else None
        except OSError:
            return None

    def model_available(self, model: str) -> bool:
        if model not in self.model_specs or self._model_path(model) is None:
            return False
        return any(_runtime_executable(path) is not None for path in _runtime_candidates())

    def set_cache_dir(self, cache_dir: str | Path) -> None:
        directory = Path(cache_dir).expanduser().resolve()
        with self._lock:
            if directory != self.cache_dir:
                self._stop()
                self.cache_dir = directory

    def _verified_model(self, model, progress=None, cancelled=None) -> Path:
        path = self._model_path(model)
        if path is None:
            raise RuntimeError("先にモデルをダウンロードしてください。")
        _notify(progress, stage="verifying", message="モデルのファイルを確認しています。")
        spec = self.model_specs[model]
        stamp = path.parent / (path.name + ".verified.json")
        identity = {
            "sha256": spec["sha256"],
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        try:
            verified = json.loads(stamp.read_text()) == identity
        except (OSError, ValueError):
            verified = False
        if not verified:
            if (
                identity["size"] != spec["weight_bytes"]
                or _sha256(path, cancelled=cancelled) != spec["sha256"]
            ):
                raise RuntimeError("モデルのファイル検証に失敗しました。再取得してください。")
            stamp.write_text(json.dumps(identity))
        return path

    def download(
        self, model: str = DEFAULT_MODEL_ID, progress: Progress = None, cancelled=None
    ) -> str:
        """Fetch/verify files without stopping the current private inference process."""
        from .download import check_download_cancelled, download_snapshot

        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        if platform.system() != "Windows":
            raise RuntimeError("この翻訳エンジンは Windows 用です。")
        check_download_cancelled(cancelled)
        ensure_runtime(progress, cancelled=cancelled)
        spec = self.model_specs[model]
        if self._model_path(model) is None:
            download_snapshot(
                spec,
                allow_patterns=[spec["filename"]],
                progress=progress,
                cache_dir=self.cache_dir,
                cancelled=cancelled,
            )
        return str(self._verified_model(model, progress, cancelled))

    def prepare(self, model: str = DEFAULT_MODEL_ID, progress: Progress = None) -> None:
        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        if platform.system() != "Windows":
            raise RuntimeError("この翻訳エンジンは Windows 用です。")
        required = self.model_specs[model]["min_memory_gb"]
        if windows_memory_gb() + 0.5 < required:
            raise RuntimeError(f"{model} はメモリ {required} GB 以上の PC で使用してください。")
        with self._lock:
            try:
                if self._loaded == model and self._process and self._process.poll() is None:
                    return
                binary = next(
                    (
                        executable
                        for directory in _runtime_candidates()
                        if (executable := _runtime_executable(directory, verify=True))
                    ),
                    None,
                )
                if binary is None:
                    raise RuntimeError("先に翻訳エンジンをダウンロードしてください。")
                path = self._verified_model(model, progress)
                self._state = "loading"
                self._start(binary, path, model, progress)
            finally:
                self._state = "idle"

    def _start(self, binary: Path, model_path: Path, model: str, progress: Progress) -> None:
        self._stop()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        api_key = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment["LLAMA_API_KEY"] = api_key
        logs = Path.home() / ".cache" / "plamo-cp"
        logs.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        args = [
            str(binary),
            "--model",
            str(model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            str(CONTEXT_SIZE),
            "--parallel",
            "1",
            "--threads",
            str(min(8, os.cpu_count() or 4)),
            "--gpu-layers",
            "0",
            "--no-context-shift",
            "--no-webui",
        ]
        try:
            _notify(progress, stage="loading", message="翻訳モデルを読み込んでいます。")
            self._process = subprocess.Popen(
                args,
                cwd=binary.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            from .bounded_log import drain_stream

            self._log_thread = threading.Thread(
                target=drain_stream,
                args=(self._process.stdout, logs / "llama-server.log"),
                daemon=True,
            )
            self._log_thread.start()
            self._client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=httpx.Timeout(600, connect=2),
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                # The setup controller can cancel a preparation through its
                # callback, including CPU model loading before it becomes ready.
                _notify(progress, stage="loading", message="翻訳モデルを読み込んでいます。")
                if self._process.poll() is not None:
                    raise RuntimeError(
                        "翻訳エンジンを起動できません。アプリのログを確認してください。"
                    )
                try:
                    response = self._client.get("/health", timeout=1)
                    if response.status_code == 200:
                        self._loaded = model
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            raise RuntimeError(
                "モデルの読み込みがタイムアウトしました。小さいモデルで再試行してください。"
            )
        except (OSError, RuntimeError):
            self._stop()
            raise

    def translate(
        self,
        text: str,
        model: str = DEFAULT_MODEL_ID,
        source_lang: str = "English",
        target_lang: str = "Japanese",
        cancelled: threading.Event | None = None,
        document_type: str = "problem",
        history: list[dict] | None = None,
        glossary: list[dict] | None = None,
        source_context: str = "",
    ) -> str:
        previous = validate_history(history)
        prompt = build_prompt(
            text,
            source_lang,
            target_lang,
            document_type=document_type,
            history=previous,
            glossary=glossary,
            source_context=source_context,
        )
        cancelled = cancelled or threading.Event()
        with self._lock:
            if cancelled.is_set():
                raise InterruptedError("翻訳を停止しました。")
            if model not in self.model_specs or not self.model_available(model):
                raise RuntimeError("先にアプリの「ダウンロード」ボタンでモデルを準備してください。")
            self.prepare(model)
            self._state = "translating"
            finished = threading.Event()
            process = self._process

            def interrupt() -> None:
                while not finished.wait(0.1):
                    if cancelled.is_set():
                        # Terminate only this owned private process. This also
                        # interrupts long CPU prefill before any token is sent.
                        if process is not None and process.poll() is None:
                            process.terminate()
                        return

            watcher = threading.Thread(target=interrupt, name="plamo-llama-cancel", daemon=True)
            watcher.start()
            try:
                tokenized = self._client.post(
                    "/tokenize",
                    json={
                        "content": prompt,
                        "add_special": False,
                        "parse_special": True,
                    },
                )
                tokenized.raise_for_status()
                tokens = tokenized.json()["tokens"]
                source_tokens = self._client.post("/tokenize", json={"content": text})
                source_tokens.raise_for_status()
                max_tokens = max(512, len(source_tokens.json()["tokens"]) * 3 + 128)
                while len(tokens) + max_tokens > CONTEXT_SIZE:
                    if not previous:
                        raise ValueError("段落が長すぎます。短く区切って翻訳してください。")
                    previous = previous[1:]
                    prompt = build_prompt(
                        text,
                        source_lang,
                        target_lang,
                        document_type=document_type,
                        history=previous,
                        glossary=glossary,
                        source_context=source_context,
                    )
                    tokenized = self._client.post(
                        "/tokenize",
                        json={
                            "content": prompt,
                            "add_special": False,
                            "parse_special": True,
                        },
                    )
                    tokenized.raise_for_status()
                    tokens = tokenized.json()["tokens"]
                payload = {
                    "prompt": tokens,
                    "n_predict": max_tokens,
                    "temperature": 0,
                    "repeat_penalty": 1.0,
                    "cache_prompt": False,
                    "stream": True,
                    "stop": ["<|plamo:tag|>", "<|plamo:bos|>"],
                }
                parts = []
                final = None
                with self._client.stream("POST", "/completion", json=payload) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if cancelled.is_set():
                            raise InterruptedError("翻訳を停止しました。")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        event = json.loads(data)
                        if "error" in event:
                            raise RuntimeError("翻訳エンジンがエラーを返しました。")
                        parts.append(event.get("content", ""))
                        if event.get("stop"):
                            final = event
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                if (
                    final is None
                    or final.get("truncated")
                    or final.get("stop_type") not in {"eos", "word"}
                ):
                    raise RuntimeError(
                        "訳文が途中で終了しました。この段落は原文を保持してください。"
                    )
                result = "".join(parts).strip()
                check_placeholders(text, result)
                return result
            except httpx.HTTPError as error:
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。") from error
                raise RuntimeError(
                    "翻訳エンジンとの接続に失敗しました。準備画面から再試行してください。"
                ) from error
            finally:
                finished.set()
                watcher.join(timeout=1)
                if process is not None and process.poll() is not None:
                    self._stop()
                self._state = "idle"

    def _stop(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._process = None
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._log_thread is not None:
            self._log_thread.join(timeout=1)
            self._log_thread = None
        self._loaded = None

    def close(self) -> None:
        with self._lock:
            self._stop()

    def unload(self, model: str | None = None) -> None:
        """Stop only this engine's live model, leaving other sizes untouched."""
        with self._lock:
            if model is not None and self._loaded != model:
                return
            self._stop()
            self._state = "idle"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare the pinned Windows CPU runtime for packaging."
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(ensure_runtime(directory=args.output))

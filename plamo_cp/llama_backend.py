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
    preview_prefix,
    translation_options,
    validate_history,
)
from .performance import LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL

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
        "revision": "e10c327b7a7ce84ffc427cb4b09360b781429fda",
        "weight_bytes": 573126848,
        "sha256": "70891ec70dc7ab4205745a38307867c2881b99239f20327927633abfe4bc9ec6",
        "min_memory_gb": 4,
    },
    "2b": {
        "revision": "228a27cc0bb4f111219963848d50a3127c16ca73",
        "weight_bytes": 1807203040,
        "sha256": "1ffd9073afcc70e35ccc942428aa2b73d7aa2b9ea779bf504b775369e0276c13",
        "min_memory_gb": 8,
    },
    "8b": {
        "revision": "fccc6b15efc7d615189fa5df6a1a02ecd2dbc18f",
        "weight_bytes": 5280445728,
        "sha256": "94e5d3f91b421f7c8900ddaac27b11a19f7441426c954bbb25a2fd60a1640187",
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
    from .marker_codec import prepare_model_input

    prepared = prepare_model_input(text, options, validate_history(history))
    text, options = prepared.text, prepared.options
    prompt = "<|plamo:bos|><|plamo:tag|>dataset<|plamo:msg|>translation"
    prompt += "<|plamo:tag|>context<|plamo:msg|>" + options["context"]
    for pair in prepared.history:
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
    supported_batch_sizes = tuple(range(1, 33))
    supported_translation_previews = True

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        from .settings import model_cache_dir

        self.cache_dir = model_cache_dir(cache_dir)
        self._lock = threading.RLock()
        self._process = None
        self._client: httpx.Client | None = None
        self._loaded: str | None = None
        self._state = "idle"
        self._log_thread = None
        self._quiet_lock = threading.RLock()
        self._quiet_level = 0
        self._cpu_limit = None
        self._configured_threads = min(8, os.cpu_count() or 4)
        self._closing = threading.Event()
        atexit.register(self.close)

    @property
    def quiet_level(self) -> int:
        with self._quiet_lock:
            return self._quiet_level

    @property
    def quiet_mode(self) -> bool:
        return self.quiet_level > 0

    def _cpu_limit_for_child(self):
        if self._cpu_limit is None:
            from .windows_cpu_limit import OwnedProcessCpuLimit

            self._cpu_limit = OwnedProcessCpuLimit(self._process, self._configured_threads)
        return self._cpu_limit

    def set_quiet_mode(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("静音モードの有効・無効を指定してください。")
        self.set_quiet_level(LEGACY_QUIET_LEVEL if enabled else 0)

    def set_quiet_level(self, level: int) -> None:
        if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
            raise ValueError(f"静音モードは 0〜{MAX_QUIET_LEVEL} で指定してください。")
        # This lock is independent of the inference lock held for a completion.
        # Limit the actual owned native process, never the SSE reader or a PID
        # reopened from the system. No child means future intent only.
        with self._quiet_lock:
            if self._process is None or self._process.poll() is not None:
                if self._cpu_limit is not None:
                    self._cpu_limit.close()
                    self._cpu_limit = None
            elif level or self._cpu_limit is not None:
                self._cpu_limit_for_child().set_level(level)
            self._quiet_level = level

    @property
    def status(self) -> dict[str, Any]:
        state = {"backend": "llama.cpp", "loaded_model": self._loaded, "state": self._state}
        if reason := getattr(self, "native_fallback_reason", None):
            state["native_fallback_reason"] = reason
        return state

    def _model_path(self, model: str) -> Path | None:
        from huggingface_hub import snapshot_download

        spec = self.model_specs[model]
        try:
            directory = snapshot_download(
                spec["repo"],
                revision=spec["revision"],
                allow_patterns=[spec["filename"]],
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
        configured_threads = min(8, os.cpu_count() or 4)
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
            "32",
            "--cont-batching",
            "--kv-unified",
            "--kv-unified-per-slot",
            str(CONTEXT_SIZE),
            "--batch-size",
            "256",
            "--ubatch-size",
            "256",
            "--cache-ram",
            "0",
            "--slots",
            "--threads",
            str(configured_threads),
            "--gpu-layers",
            "0",
            "--no-context-shift",
            "--no-webui",
        ]
        try:
            _notify(progress, stage="loading", message="翻訳モデルを読み込んでいます。")
            child = subprocess.Popen(
                args,
                cwd=binary.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            with self._quiet_lock:
                self._process = child
                self._configured_threads = configured_threads
                if self._quiet_level:
                    self._cpu_limit_for_child().set_level(self._quiet_level)
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

    def _new_request(self, cancelled):
        from .llama_jobs import RequestControl

        return RequestControl(
            self._client.base_url, self._client.headers, cancelled, shutdown=self._closing
        )

    def _prepare_completion(
        self,
        text,
        control,
        source_lang="English",
        target_lang="Japanese",
        document_type="problem",
        history=None,
        glossary=None,
        source_context="",
    ):
        from .llama_jobs import PreparedCompletion
        from .marker_codec import prepare_model_input

        previous = validate_history(history)

        def tokenize(value, **options):
            control.check()
            value = control.post_json("/tokenize", {"content": value, **options})
            tokens = value.get("tokens")
            if not isinstance(tokens, list) or any(type(token) is not int for token in tokens):
                raise RuntimeError("翻訳エンジンのトークンを解釈できませんでした。")
            return tokens

        def prompt():
            return build_prompt(
                text,
                source_lang,
                target_lang,
                document_type=document_type,
                history=previous,
                glossary=glossary,
                source_context=source_context,
            )

        tokens = tokenize(prompt(), add_special=False, parse_special=True)
        budget = max(512, len(tokenize(text)) * 3 + 128)
        while len(tokens) + budget > CONTEXT_SIZE:
            if not previous:
                raise ValueError("段落が長すぎます。短く区切って翻訳してください。")
            previous = previous[1:]
            tokens = tokenize(prompt(), add_special=False, parse_special=True)
        options = translation_options(
            text,
            source_lang,
            target_lang,
            document_type=document_type,
            glossary=glossary,
            source_context=source_context,
        )
        payload = {
            "prompt": tokens,
            "n_predict": budget,
            "temperature": 0,
            "repeat_penalty": 1.0,
            "cache_prompt": False,
            "stream": True,
            "stop": ["<|plamo:tag|>", "<|plamo:bos|>"],
        }
        return PreparedCompletion(text, payload, prepare_model_input(text, options, previous).codec)

    def _completion(self, prepared, control, progress=None):
        parts, final = [], None
        preview, preview_at, preview_enabled = "", float("-inf"), progress is not None
        length = 0
        for event in control.events("/completion", prepared.payload):
            control.check()
            if "error" in event:
                raise RuntimeError("翻訳エンジンがエラーを返しました。")
            content = event.get("content", "")
            if not isinstance(content, str):
                raise RuntimeError("翻訳エンジンの応答を解釈できませんでした。")  # noqa: TRY004
            parts.append(content)
            length += len(content)
            if length > 2 * 1024 * 1024:
                raise RuntimeError("翻訳エンジンの応答が大きすぎます。")
            if event.get("stop"):
                final = event
            if preview_enabled and final is None:
                # Keep snapshots bounded independently of final generation.
                # A partial wire tag is withheld by the same codec used on Mac.
                preview = (preview + content)[:16384]
                now = time.monotonic()
                if now - preview_at >= 0.05:
                    preview_at = now
                    try:
                        draft = prepared.codec.decode(preview_prefix(preview), partial=True)
                        if draft.strip():
                            progress(draft[:16384])
                    except Exception:  # noqa: BLE001 - previews cannot break final inference
                        preview_enabled = False
                        log.debug("Disabled a translation preview after its callback failed")
        control.check()
        if final is None or final.get("truncated") or final.get("stop_type") not in {"eos", "word"}:
            raise RuntimeError("訳文が途中で終了しました。この段落は原文を保持してください。")
        translated = prepared.codec.decode("".join(parts).strip())
        check_placeholders(prepared.text, translated)
        return translated

    def _ensure_prepared(self, model):
        if model not in self.model_specs or not self.model_available(model):
            raise RuntimeError("先にアプリの「ダウンロード」ボタンでモデルを準備してください。")
        self.prepare(model)
        self._state = "translating"

    def iter_work(
        self,
        model,
        take,
        capacity,
        *,
        cancelled=None,
        prefill_batch_size=1,
        prefill_step_size=256,
    ):
        from .llama_jobs import AnyCancelled, NativeReleaseError, iter_completions

        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        # The pinned native runtime chunks prefill at startup (-b/-ub 256).
        # Client request count never multiplies the shared 32768-token KV pool.
        cancelled = AnyCancelled(cancelled or threading.Event(), self._closing)
        loaded = False

        def make_request(work):
            nonlocal loaded
            if not loaded:
                self._ensure_prepared(model)
                loaded = True
            return self._new_request(AnyCancelled(work.cancelled, cancelled))

        def prepare(work, control):
            try:
                return self._prepare_completion(work.text, control, **work.parameters)
            except RuntimeError as error:
                if self._process is None or self._process.poll() is not None:
                    raise
                raise ValueError(str(error)) from error

        def complete(work, prepared, control):
            try:
                try:
                    return self._completion(prepared, control, work.progress)
                finally:
                    if finish := getattr(control, "finish", None):
                        finish()
            except NativeReleaseError:
                raise
            except RuntimeError as error:
                if self._process is None or self._process.poll() is not None:
                    raise
                # Per-request errors cannot discard another client's valid work.
                raise ValueError(str(error)) from error

        with self._lock:
            try:
                yield from iter_completions(
                    take, capacity, prepare, complete, make_request, cancelled, CONTEXT_SIZE
                )
            except NativeReleaseError:
                self._stop()
                raise
            finally:
                if self._process is not None and self._process.poll() is not None:
                    self._stop()
                self._state = "idle"

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
        from .llama_jobs import AnyCancelled, NativeReleaseError, RequestGroup

        cancelled = AnyCancelled(cancelled or threading.Event(), self._closing)
        with self._lock:
            if cancelled.is_set():
                raise InterruptedError("翻訳を停止しました。")
            self._ensure_prepared(model)
            try:
                with RequestGroup() as group:
                    control = self._new_request(cancelled)
                    group.add(control)
                    prepared = self._prepare_completion(
                        text,
                        control,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        document_type=document_type,
                        history=history,
                        glossary=glossary,
                        source_context=source_context,
                    )
                    prepared.payload["id_slot"] = 0
                    try:
                        return self._completion(prepared, control)
                    finally:
                        if finish := getattr(control, "finish", None):
                            finish()
            except NativeReleaseError:
                self._stop()
                raise
            finally:
                if self._process is not None and self._process.poll() is not None:
                    self._stop()
                self._state = "idle"

    def _stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        with self._quiet_lock:
            self._loaded = None
            if self._cpu_limit is not None:
                self._cpu_limit.close()
                self._cpu_limit = None
            self._process = None
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._log_thread is not None:
            self._log_thread.join(timeout=1)
            self._log_thread = None
        self._loaded = None

    def close(self) -> None:
        self._closing.set()
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

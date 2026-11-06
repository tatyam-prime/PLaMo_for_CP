"""Bandwidth-limited Hugging Face downloads, including metadata and redirects.

The HTTP response stream is paced before each 64 KiB read. This applies network
backpressure instead of downloading a complete response and slowing disk writes.
TCP/TLS buffers may read ahead by a small amount; no large file is prefetched by
this wrapper. Xet is disabled because its Rust downloader bypasses httpx.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import os
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
DEFAULT_RATE_MIB = None
READ_QUANTUM = 64 * 1024
DOWNLOAD_LOCK = threading.Lock()
ALLOW_PATTERNS = [
    "*.safetensors",
    "*.json",
    "*.jsonl",
    "*.jinja",
    "modeling_plamo_mlx.py",
    "tokenization_plamo.py",
    "LICENSE*",
    "LICENSE/*",
]
READY_FILE = ".plamo-cp-ready-v1.json"


def resolve_rate(rate_mib: float | None = None) -> float | None:
    """Optional developer/session override; distributed apps use normal networking."""
    value = (
        rate_mib
        if rate_mib is not None
        else os.environ.get("PLAMO_DOWNLOAD_MIBPS", DEFAULT_RATE_MIB)
    )
    if value is None:
        return None
    try:
        rate = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError("PLAMO_DOWNLOAD_MIBPS must be a finite positive MiB/s rate.") from error
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Download rate override must be finite and positive.")
    return rate


class BandwidthLimiter:
    """Shared pacing budget; idle time never accumulates into a burst."""

    def __init__(
        self,
        rate_mib: float | None,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        progress: Callable[[dict], None] | None = None,
        cancelled: threading.Event | None = None,
    ):
        rate = resolve_rate(rate_mib)
        self.bytes_per_second = rate * 1024 * 1024 if rate is not None else None
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_read = clock()
        self._progress = progress
        self.cancelled = cancelled
        self.received_bytes = 0

    def check(self) -> None:
        check_download_cancelled(self.cancelled)

    def received(self, byte_count: int) -> None:
        self.check()
        self.received_bytes += byte_count
        if self._progress:
            self._progress({"stage": "downloading", "bytes_received": self.received_bytes})

    def wait_for(self, byte_count: int) -> None:
        self.check()
        if byte_count <= 0 or self.bytes_per_second is None:
            return
        with self._lock:
            now = self._clock()
            self._next_read = max(now, self._next_read) + byte_count / self.bytes_per_second
            delay = self._next_read - now
            if delay > 0:
                if self.cancelled is not None:
                    self.cancelled.wait(delay)
                    self.check()
                else:
                    self._sleep(delay)


class RateLimitedStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, limiter: BandwidthLimiter):
        self._stream = stream
        self._limiter = limiter

    def __iter__(self):
        iterator = iter(self._stream)
        while True:
            # HTTP/1.1 httpcore reads at most 64 KiB at a time. Reserve that
            # capacity before advancing its iterator, including the first read.
            self._limiter.wait_for(READ_QUANTUM)
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            if len(chunk) > READ_QUANTUM:
                # Defensive accounting for a future/custom transport yielding
                # bigger chunks; regular HTTPTransport never takes this branch.
                self._limiter.wait_for(len(chunk) - READ_QUANTUM)
            self._limiter.received(len(chunk))
            yield chunk

    def close(self):
        self._stream.close()


class RateLimitedTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, limiter: BandwidthLimiter):
        self._transport = transport
        self._limiter = limiter

    def handle_request(self, request):
        self._limiter.check()
        if self._limiter.cancelled is not None:
            timeouts = dict(request.extensions.get("timeout") or {})
            for phase in ("connect", "read", "write", "pool"):
                value = timeouts.get(phase)
                timeouts[phase] = min(10.0, value) if isinstance(value, (int, float)) else 10.0
            request.extensions["timeout"] = timeouts
        response = self._transport.handle_request(request)
        try:
            self._limiter.check()
        except InterruptedError:
            response.close()
            raise
        if self._limiter.cancelled is not None:
            retry = response.headers.get("Retry-After", "").strip()
            long_retry = retry.isascii() and retry.isdigit() and (len(retry) > 8 or int(retry) > 8)
            if response.status_code == 429 or (response.status_code >= 400 and long_retry):
                # Hub backoff uses an uninterruptible sleep for server-directed
                # rate limits. Leave partial files resumable instead of making
                # cancellation/app shutdown wait for that potentially long sleep.
                response.close()
                raise RuntimeError(
                    "配布元が混雑しています。少し時間をおいて、ダウンロードを再開してください。"
                )
        response.stream = RateLimitedStream(response.stream, self._limiter)
        return response

    def close(self):
        self._transport.close()


def limited_client_factory(limiter: BandwidthLimiter):
    """Keep HF defaults/proxies, wrapping every transport so none bypass pacing."""
    from huggingface_hub.utils._http import default_client_factory

    client = default_client_factory()
    # httpx has no public hook covering both its direct and environment-proxy
    # transports. These attributes are verified against the locked httpx version.
    wrappers = {}

    def wrap(transport):
        if transport is None:
            return None
        # Bound TCP read-ahead too, rather than letting the OS buffer several
        # MiB while Python waits. Linux may double this socket buffer internally.
        if limiter.bytes_per_second is not None:
            pool = transport._pool
            pool._socket_options = list(pool._socket_options or []) + [
                (socket.SOL_SOCKET, socket.SO_RCVBUF, READ_QUANTUM)
            ]
        return wrappers.setdefault(id(transport), RateLimitedTransport(transport, limiter))

    client._transport = wrap(client._transport)
    client._mounts = {pattern: wrap(transport) for pattern, transport in client._mounts.items()}
    return client


def check_download_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("ダウンロードを中断しました。次回、続きから取得できます。")


@contextmanager
def _download_lock(cancelled: threading.Event | None = None):
    """Serialize writers, but let a cancelled waiter leave within100ms."""
    from filelock import FileLock, Timeout

    path = Path.home() / ".cache" / "plamo-cp" / "download.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    while not DOWNLOAD_LOCK.acquire(timeout=0.1):
        check_download_cancelled(cancelled)
    file_lock = FileLock(path)
    acquired = False
    try:
        while not acquired:
            check_download_cancelled(cancelled)
            try:
                file_lock.acquire(timeout=0.1)
                acquired = True
            except Timeout:
                continue
        yield
    finally:
        if acquired:
            file_lock.release()
        DOWNLOAD_LOCK.release()


def _ready(path: Path, spec: dict[str, Any], patterns: list[str]) -> bool:
    try:
        manifest = json.loads((path / READY_FILE).read_text(encoding="utf-8"))
        if (
            manifest["repo"] != spec["repo"]
            or manifest["revision"] != spec["revision"]
            or manifest["patterns"] != patterns
        ):
            return False
        return bool(manifest["files"]) and all(
            (path / name).is_file() and (path / name).stat().st_size == size
            for name, size in manifest["files"].items()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _inference_files_ready(path: Path) -> bool:
    """Recognize complete older snapshots downloaded before manifests existed."""
    required = [
        "config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.jsonl",
        "chat_template.jinja",
        "modeling_plamo_mlx.py",
        "tokenization_plamo.py",
    ]
    try:
        index = path / "model.safetensors.index.json"
        if index.is_file():
            weights = json.loads(index.read_text(encoding="utf-8"))["weight_map"].values()
            required.extend(set(weights))
            if not weights:
                return False
        else:
            required.append("model.safetensors")
        return all(
            (path / name).is_file() and (path / name).stat().st_size > 0 for name in required
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def cached_snapshot(
    spec: dict[str, Any],
    patterns: list[str] | None = None,
    *,
    cache_dir: str | Path | None = None,
) -> str | None:
    """Inspect the local cache only. Never starts a download or creates a client."""
    from huggingface_hub import snapshot_download

    selected = ALLOW_PATTERNS if patterns is None else patterns
    try:
        path = Path(
            snapshot_download(
                repo_id=spec["repo"],
                revision=spec["revision"],
                allow_patterns=selected,
                local_files_only=True,
                **({"cache_dir": str(cache_dir)} if cache_dir is not None else {}),
            )
        )
    except OSError:
        return None
    if _ready(path, spec, selected) or (
        selected == ALLOW_PATTERNS and _inference_files_ready(path)
    ):
        return str(path)
    # A single pinned GGUF file can also be recognized before manifests existed.
    if selected and all(
        "*" not in name and (path / name).is_file() and (path / name).stat().st_size > 0
        for name in selected
    ):
        return str(path)
    return None


def _mark_ready(path: Path, spec: dict[str, Any], patterns: list[str]) -> None:
    files = {
        file.relative_to(path).as_posix(): file.stat().st_size
        for file in path.rglob("*")
        if file.is_file()
        and file.name != READY_FILE
        and any(fnmatch.fnmatch(file.relative_to(path).as_posix(), pattern) for pattern in patterns)
    }
    manifest = {
        "repo": spec["repo"],
        "revision": spec["revision"],
        "patterns": patterns,
        "files": files,
    }
    temporary = path / (READY_FILE + ".tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    temporary.replace(path / READY_FILE)


def download_snapshot(
    spec: dict[str, Any],
    rate_mib: float | None = None,
    *,
    cache_dir: str | Path | None = None,
    allow_patterns: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
    cancelled: threading.Event | None = None,
) -> str:
    """Fetch a pinned snapshot; any session bandwidth override stays internal."""
    from huggingface_hub import constants, set_client_factory, snapshot_download

    rate = resolve_rate(rate_mib)
    patterns = ALLOW_PATTERNS if allow_patterns is None else allow_patterns
    options = {
        "repo_id": spec["repo"],
        "revision": spec["revision"],
        "allow_patterns": patterns,
        "max_workers": 1,
    }
    if cache_dir is not None:
        options["cache_dir"] = str(cache_dir)
    with _download_lock(cancelled):
        check_download_cancelled(cancelled)
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        constants.HF_HUB_DISABLE_XET = True
        try:
            path = Path(snapshot_download(**options, local_files_only=True))
        except OSError:
            path = None
        if path is not None and (
            _ready(path, spec, patterns)
            or (patterns == ALLOW_PATTERNS and _inference_files_ready(path))
        ):
            if progress:
                progress({"stage": "downloaded", "bytes_received": 0, "cached": True})
            return str(path)
        limiter = BandwidthLimiter(rate, progress=progress, cancelled=cancelled)
        from huggingface_hub.utils import _http

        previous_factory = _http._GLOBAL_CLIENT_FACTORY
        set_client_factory(lambda: limited_client_factory(limiter))
        log.info(
            "Model download: %s; one file at a time",
            f"session cap {rate:g} MiB/s" if rate is not None else "normal network speed",
        )
        try:
            path = Path(snapshot_download(**options))
            check_download_cancelled(cancelled)
            _mark_ready(path, spec, patterns)
            if progress:
                progress({"stage": "downloaded", "bytes_received": limiter.received_bytes})
            return str(path)
        finally:
            # Cancellation/progress belong only to this transfer, never to a
            # later cached model's tokenizer or another request.
            set_client_factory(previous_factory)

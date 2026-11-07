"""Parent-side IPC adapter isolating native GPU failures in an owned child."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .engine import (
    DEFAULT_MODEL_ID,
    TranslationWork,
    check_placeholders,
    translation_options,
    validate_history,
)
from .llama_backend import LlamaEngine, _notify, windows_memory_gb
from .native_ipc import (
    MAX_SOURCE_CHARACTERS,
    VERSION,
    ProtocolError,
    fields,
    identifier,
    integer,
    read_frame,
    text,
    write_frame,
)
from .performance import LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL


def worker_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--native-worker"]
    return [sys.executable, "-m", "plamo_cp.native_worker"]


class NativeEngine(LlamaEngine):
    def __init__(self, runtime, cache_dir=None):
        super().__init__(cache_dir)
        self.runtime = runtime
        self._ipc = threading.Condition()
        self._writer = threading.Lock()
        self._reader_thread = None
        self._pending = {}
        self._cancel_sent = set()
        self._results = deque()
        self._previews = {}
        self._quiet_requests = {}
        self._terminal_received = set()
        self._native_error = None
        self._ready = False
        self._native_info = {}
        self._fallback = None
        self._native_disabled_reason = None

    @property
    def status(self):
        if self._fallback is not None:
            return {
                **self._fallback.status,
                "native_fallback_reason": self._native_disabled_reason,
            }
        return {
            "backend": "llama.cpp-native",
            "loaded_model": self._loaded,
            "state": self._state,
            "native": dict(self._native_info),
        }

    def model_available(self, model):
        if self._fallback is not None:
            return self._fallback.model_available(model)
        return (
            model in self.model_specs
            and self._model_path(model) is not None
            and self.runtime.bridge.is_file()
        )

    def _send(self, frame):
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("GPU 推論プロセスが終了しました。再準備してください。")
        try:
            with self._writer:
                write_frame(process.stdin, frame)
        except (OSError, ProtocolError) as error:
            raise RuntimeError("GPU 推論プロセスとの接続が終了しました。") from error

    def _receive(self, process):
        try:
            while frame := read_frame(process.stdout):
                kind = frame["type"]
                with self._ipc:
                    if process is not self._process:
                        return
                    if kind == "ready":
                        fields(frame, {"type", "version", "info"})
                        integer(frame["version"], VERSION, VERSION)
                        if self._ready or not isinstance(frame["info"], dict):
                            raise ProtocolError("GPU 推論プロセスの起動応答が不正です。")
                        if len(json.dumps(frame["info"])) > 32768:
                            raise ProtocolError("GPU 推論プロセスの情報が長すぎます。")
                        self._native_info = dict(frame["info"])
                        self._ready = True
                    elif kind == "quiet_applied":
                        fields(frame, {"type", "id", "level"})
                        key = identifier(frame["id"])
                        level = integer(frame["level"], 0, MAX_QUIET_LEVEL)
                        if key not in self._quiet_requests:
                            raise ProtocolError("GPU 推論プロセスの設定応答が不正です。")
                        self._quiet_requests[key] = level
                    elif kind == "fatal":
                        fields(frame, {"type", "message"})
                        self._native_error = text(frame["message"], 1024)
                    else:
                        key = identifier(frame.get("id"))
                        if key not in self._pending or key in self._terminal_received:
                            raise ProtocolError("GPU 推論プロセスの段落識別子が不正です。")
                        if kind in {"preview", "result"}:
                            fields(frame, {"type", "id", "text"})
                            text(frame["text"], 16384 if kind == "preview" else 131072)
                            if kind == "preview":
                                self._previews[key] = frame["text"]
                            else:
                                self._results.append(frame)
                                self._terminal_received.add(key)
                        elif kind == "error":
                            fields(frame, {"type", "id", "message"})
                            text(frame["message"], 1024)
                            self._results.append(frame)
                            self._terminal_received.add(key)
                        elif kind == "cancelled":
                            fields(frame, {"type", "id"})
                            self._results.append(frame)
                            self._terminal_received.add(key)
                        else:
                            raise ProtocolError("GPU 推論プロセスの応答が不正です。")
                        if len(self._results) > 64:
                            raise ProtocolError("GPU 推論プロセスの応答件数が多すぎます。")
                    self._ipc.notify_all()
        except (OSError, ProtocolError):
            with self._ipc:
                if process is self._process:
                    self._native_error = (
                        "GPU 推論プロセスとの通信が終了しました。再準備してください。"
                    )
                    self._ipc.notify_all()
        finally:
            with self._ipc:
                if process is self._process:
                    self._native_error = self._native_error or "GPU 推論プロセスが終了しました。"
                    self._ipc.notify_all()

    def prepare(self, model=DEFAULT_MODEL_ID, progress=None):
        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        if platform.system() != "Windows":
            raise RuntimeError("この翻訳エンジンは Windows 用です。")
        if windows_memory_gb() + 0.5 < self.model_specs[model]["min_memory_gb"]:
            raise RuntimeError(
                "モデルを読み込むメモリが足りません。小さいモデルを選択してください。"
            )
        with self._lock:
            if self._fallback is not None:
                self._fallback.prepare(model, progress)
                self._loaded = self._fallback.status["loaded_model"]
                return
            if self._native_disabled_reason:
                self._cpu_fallback(model, progress)
                return
            if self._loaded == model and self._process and self._process.poll() is None:
                return
            path = self._verified_model(model, progress)
            try:
                self._state = "loading"
                self._start_native(path, model, progress)
            except InterruptedError:
                raise
            except (RuntimeError, OSError):
                if self._closing.is_set():
                    raise
                self._native_disabled_reason = (
                    "GPU 推論エンジンを起動できなかったため、CPU で実行しています。"
                )
                self._stop()
                self._cpu_fallback(model, progress)
            finally:
                self._state = "idle"

    def _cpu_fallback(self, model, progress):
        # This path uses the separately archive-verified CPU server, never an
        # unverified native DLL. Its prepare action cannot download weights.
        fallback = LlamaEngine(self.cache_dir)
        fallback.native_fallback_reason = self._native_disabled_reason
        fallback.set_quiet_level(self.quiet_level)
        try:
            fallback.prepare(model, progress)
        except BaseException:
            fallback.close()
            raise
        self._fallback = fallback
        self._loaded = fallback.status["loaded_model"]

    def set_cache_dir(self, cache_dir):
        super().set_cache_dir(cache_dir)
        if self._fallback is not None:
            self._fallback.set_cache_dir(cache_dir)

    def unload(self, model=None):
        with self._lock:
            if model is not None and self.status["loaded_model"] != model:
                return
            self._stop()
            self._state = "idle"

    def _start_native(self, path, model, progress):
        self._stop()
        if self._closing.is_set():
            raise InterruptedError("翻訳アプリを終了しています。")
        with self._ipc:
            self._native_error, self._ready = None, False
            self._native_info = {}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            bufsize=0,
        )
        try:
            with self._quiet_lock:
                self._process = process
                self._reader_thread = threading.Thread(
                    target=self._receive, args=(process,), name="plamo-native-reader", daemon=True
                )
                self._reader_thread.start()
                from .bounded_log import drain_stream

                self._log_thread = threading.Thread(
                    target=drain_stream,
                    args=(process.stderr, Path.home() / ".cache/plamo-cp/native-worker.log"),
                    name="plamo-native-stderr",
                    daemon=True,
                )
                self._log_thread.start()
                self._send(
                    {
                        "type": "init",
                        "version": VERSION,
                        "model_path": str(path),
                        "dll_dir": str(self.runtime.directory),
                        "bridge_path": str(self.runtime.bridge),
                        "threads": min(8, os.cpu_count() or 4),
                        "quiet_level": self._quiet_level,
                    }
                )
        except BaseException:
            self._stop()
            raise
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                _notify(progress, stage="loading", message="GPU 翻訳モデルを読み込んでいます。")
                with self._ipc:
                    if self._native_error:
                        raise RuntimeError(self._native_error)
                    if self._ready:
                        self._loaded = model
                        return
                    self._ipc.wait(0.1)
                if self._closing.is_set():
                    raise InterruptedError("翻訳アプリを終了しています。")
            raise RuntimeError("GPU 翻訳モデルの読み込みがタイムアウトしました。")
        except BaseException:
            self._stop()
            raise

    def set_quiet_level(self, level):
        if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
            raise ValueError(f"静音モードは 0〜{MAX_QUIET_LEVEL} で指定してください。")
        with self._quiet_lock:
            if self._fallback is not None:
                self._fallback.set_quiet_level(level)
                self._quiet_level = level
                return
            if self._process is not None and self._process.poll() is None:
                key = uuid.uuid4().hex
                with self._ipc:
                    self._quiet_requests[key] = None
                try:
                    self._send({"type": "quiet", "id": key, "level": level})
                    deadline = time.monotonic() + 5
                    with self._ipc:
                        while self._quiet_requests[key] is None and not self._native_error:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise RuntimeError("推論の稼働率の変更を確認できませんでした。")
                            self._ipc.wait(min(remaining, 0.1))
                        if self._native_error or self._quiet_requests[key] != level:
                            raise RuntimeError("推論の稼働率を変更できませんでした。")
                except RuntimeError:
                    with self._ipc:
                        self._native_error = (
                            "推論の稼働率を確認できないため、GPU 推論を停止しました。"
                        )
                        self._ipc.notify_all()
                    if self._process is not None and self._process.poll() is None:
                        self._process.terminate()
                    raise
                finally:
                    with self._ipc:
                        self._quiet_requests.pop(key, None)
            self._quiet_level = level

    def set_quiet_mode(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("静音モードの有効・無効を指定してください。")
        self.set_quiet_level(LEGACY_QUIET_LEVEL if enabled else 0)

    def iter_work(self, model, take, capacity, *, cancelled=None, **_prefill):
        from .llama_jobs import AnyCancelled

        cancelled = AnyCancelled(cancelled or threading.Event(), self._closing)
        with self._lock:
            try:
                if self._fallback is not None:
                    yield from self._fallback.iter_work(
                        model, take, capacity, cancelled=cancelled, **_prefill
                    )
                    return
                started = False
                while True:
                    if cancelled.is_set() or self._closing.is_set():
                        raise InterruptedError("翻訳を停止しました。")
                    with self._ipc:
                        if self._native_error:
                            raise RuntimeError(self._native_error)
                        previews, self._previews = self._previews, {}
                        results, self._results = self._results, deque()
                    for key, draft in previews.items():
                        work = self._pending.get(key)
                        if work and not work.cancelled.is_set() and work.progress is not None:
                            try:
                                work.progress(draft)
                            except Exception:  # noqa: BLE001 - previews cannot break generation
                                work.progress = None
                    for frame in results:
                        key = frame["id"]
                        with self._ipc:
                            work = self._pending.pop(key)
                            self._terminal_received.discard(key)
                            self._cancel_sent.discard(key)
                            self._previews.pop(key, None)
                        if work.cancelled.is_set() or frame["type"] == "cancelled":
                            result = InterruptedError("翻訳を停止しました。")
                        elif frame["type"] == "error":
                            result = ValueError(frame["message"])
                        else:
                            try:
                                result = frame["text"]
                                check_placeholders(work.text, result)
                            except ValueError as error:
                                result = error
                        yield work, result
                    for key, work in list(self._pending.items()):
                        if work.cancelled.is_set() and key not in self._cancel_sent:
                            self._send({"type": "cancel", "id": key})
                            self._cancel_sent.add(key)
                    limit = min(32, max(1, int(capacity())))
                    if len(self._pending) < limit:
                        works = take(1)
                        if len(works) > 1:
                            raise RuntimeError("翻訳キューの同時処理数が一致しません。")
                        if works:
                            work = works[0]
                            if work.cancelled.is_set():
                                yield work, InterruptedError("翻訳を停止しました。")
                                continue
                            try:
                                text(work.text, MAX_SOURCE_CHARACTERS)
                                work.text.encode("utf-8")
                                parameters = dict(work.parameters)
                                parameters.setdefault("source_lang", "English")
                                parameters.setdefault("target_lang", "Japanese")
                                history = validate_history(parameters.pop("history", None))
                                translation_options(work.text, **parameters)
                                json.dumps([parameters, history], ensure_ascii=False).encode(
                                    "utf-8"
                                )
                            except (ValueError, TypeError, ProtocolError, UnicodeError):
                                yield work, ValueError("この段落の文字列または翻訳設定が不正です。")
                                continue
                            if not started:
                                self._ensure_prepared(model)
                                started = True
                                if self._fallback is not None:
                                    first = [work]

                                    def fallback_take(count, first=first):
                                        return [first.pop()] if first else take(count)

                                    yield from self._fallback.iter_work(
                                        model,
                                        fallback_take,
                                        capacity,
                                        cancelled=cancelled,
                                        **_prefill,
                                    )
                                    return
                            key = uuid.uuid4().hex
                            with self._ipc:
                                self._pending[key] = work
                            self._send(
                                {
                                    "type": "submit",
                                    "id": key,
                                    "text": work.text,
                                    "parameters": work.parameters,
                                }
                            )
                            continue
                    if not self._pending:
                        return
                    with self._ipc:
                        self._ipc.wait(0.01)
            except RuntimeError:
                if self._fallback is None:
                    self._native_disabled_reason = (
                        "GPU 推論プロセスが終了したため、次回から CPU で実行します。"
                    )
                self._stop()
                raise
            finally:
                if self._pending:
                    # Iterator shutdown belongs to global close/fatal paths;
                    # per-job Stop is handled above without stopping the child.
                    self._stop()
                self._state = "idle"

    def translate(self, value, model=DEFAULT_MODEL_ID, cancelled=None, **parameters):
        work = TranslationWork("single", value, parameters, cancelled or threading.Event())
        pending = [work]
        stream = self.iter_work(
            model, lambda _: [pending.pop(0)] if pending else [], lambda: 1, cancelled=cancelled
        )
        try:
            for _work, result in stream:
                if isinstance(result, Exception):
                    raise result
                return result
        finally:
            stream.close()
        raise RuntimeError("訳文の生成を確認できませんでした。")

    def _stop(self):
        if self._fallback is not None:
            with self._fallback._lock:
                self._fallback._stop()
        process = self._process
        if process is not None:
            if process.poll() is None:
                try:
                    self._send({"type": "close"})
                    process.wait(timeout=5)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        self._process = None
        for thread in (self._reader_thread, self._log_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)
        self._reader_thread = self._log_thread = None
        with self._ipc:
            self._pending.clear()
            self._previews.clear()
            self._results.clear()
            self._cancel_sent.clear()
            self._terminal_received.clear()
            self._native_error, self._ready = None, False
            self._native_info = {}
        self._loaded = None

"""Cancellable private HTTP requests and bounded Windows completion workers.

The native server owns continuous batching. Python admits work in FCFS order
only while the shared KV budget can reserve the full prompt and output budget.
Each request owns its socket, so cancellation never closes another request or
terminates the shared model process.
"""

from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .http_cancel import CancellableSocket

MAX_PARALLEL = 32
MAX_HTTP_BYTES = 2 * 1024 * 1024
RELEASE_TIMEOUT = 30


class NativeReleaseError(RuntimeError):
    """Native KV ownership is uncertain; do not silently reuse its reservation."""


class AnyCancelled:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout):
        return self.is_set() or self.events[0].wait(timeout) or self.is_set()


class RequestControl:
    """One request's connection, captured before waiting for HTTP headers."""

    def __init__(self, base_url, headers, cancelled, *, shutdown=None):
        address = urlsplit(str(base_url))
        if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port:
            raise ValueError("翻訳エンジンの接続先が正しくありません。")
        self.port = address.port
        self.base_url = base_url
        self.headers = {"Content-Type": "application/json"}
        if authorization := headers.get("authorization") or headers.get("Authorization"):
            self.headers["Authorization"] = authorization
        # No X-Conversation-Id: native resumable streams deliberately survive
        # HTTP disconnects, whereas our requests must release their own slot.
        self.cancelled = cancelled
        self.shutdown = shutdown or threading.Event()
        self._aborted = threading.Event()
        self._disconnect = threading.Event()
        self._lock = threading.Lock()
        self._connection = None
        self._socket = None
        self._slot = None
        self._completion_sent = False
        self._started = False
        self._cancel_at = None
        self._unsafe_release = False

    def check(self):
        if self._aborted.is_set() or self.cancelled.is_set():
            raise InterruptedError("翻訳を停止しました。")

    def cancel(self):
        self._aborted.set()
        with self._lock:
            if self._completion_sent and not self._started and not self.shutdown.is_set():
                # b12119 returns HTTP 200 when the slot has started, before
                # prompt decode. Before that ACK, GET /slots can overtake the
                # queued task. Retain the connection and KV reservation until
                # ACK rather than mistaking an initially idle slot for release.
                self._cancel_at = self._cancel_at or time.monotonic()
                if time.monotonic() - self._cancel_at < RELEASE_TIMEOUT:
                    return
                self._unsafe_release = True
            stream, connection = self._socket, self._connection
        self._disconnect.set()
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    @contextmanager
    def _response(self, path, payload, *, method="POST", read_timeout=600):
        if (method, path) not in {
            ("POST", "/tokenize"),
            ("POST", "/completion"),
            ("GET", "/slots"),
        }:
            raise ValueError("翻訳エンジンの要求先が正しくありません。")
        self.check()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        response = None
        with self._lock:
            self._connection = connection
        try:
            connection.connect()
            connection.auto_open = 0  # A cancellation must never silently reconnect.
            with self._lock:
                self.check()
                # 未 ACK の completion は、KV 予約を安全に解放できるまで切断しない。
                connection.sock = CancellableSocket(connection.sock, self._disconnect.is_set)
                self._socket = connection.sock
            self.check()
            connection.sock.settimeout(read_timeout)
            body = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                if payload is not None
                else None
            )
            if path == "/completion":
                with self._lock:
                    self.check()
                    self._slot = payload["id_slot"]
                    self._completion_sent = True
            connection.request(method, path, body=body, headers=self.headers)
            response = connection.getresponse()
            if path == "/completion" and response.status == 200:
                with self._lock:
                    self._started = True
            self.check()
            if response.status != 200:
                raise RuntimeError("翻訳エンジンが要求を処理できませんでした。")
            yield response
        except (OSError, http.client.HTTPException) as error:
            self.check()
            raise RuntimeError("翻訳エンジンとの接続に失敗しました。") from error
        finally:
            if response is not None:
                response.close()
            connection.close()
            with self._lock:
                if self._connection is connection:
                    self._connection = self._socket = None

    def finish(self):
        """A reservation ends only after its started native slot becomes idle."""
        if self.shutdown.is_set():
            return  # The owner will terminate the whole child after worker join.
        if self._unsafe_release or (self._completion_sent and not self._started):
            raise NativeReleaseError("翻訳エンジンの停止を確認できません。再準備してください。")
        if not self._started:
            return
        deadline = time.monotonic() + RELEASE_TIMEOUT
        observer = RequestControl(self.base_url, self.headers, self.shutdown)
        try:
            while not self.shutdown.is_set() and time.monotonic() < deadline:
                with observer._response("/slots", None, method="GET", read_timeout=2) as response:
                    body = response.read(MAX_HTTP_BYTES + 1)
                    if len(body) > MAX_HTTP_BYTES:
                        raise ValueError("Slot response too large")
                    slots = json.loads(body)
                if not isinstance(slots, list):
                    raise TypeError("Invalid slot snapshot")
                if not all(isinstance(slot, dict) for slot in slots):
                    raise TypeError("Invalid slot snapshot")
                slot = next((slot for slot in slots if slot.get("id") == self._slot), None)
                if slot is None or type(slot.get("is_processing")) is not bool:
                    raise ValueError("Invalid slot snapshot")
                if not slot["is_processing"]:
                    return
                self.shutdown.wait(0.02)
        except (RuntimeError, ValueError, TypeError) as error:
            raise NativeReleaseError(
                "翻訳エンジンの停止を確認できません。再準備してください。"
            ) from error
        finally:
            observer.cancel()
        if not self.shutdown.is_set():
            raise NativeReleaseError("翻訳エンジンの停止を確認できません。再準備してください。")

    def post_json(self, path, payload):
        with self._response(path, payload) as response:
            body = response.read(MAX_HTTP_BYTES + 1)
            self.check()
            if len(body) > MAX_HTTP_BYTES:
                raise RuntimeError("翻訳エンジンの応答が大きすぎます。")
            try:
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise TypeError
                return value
            except (ValueError, TypeError, UnicodeError) as error:
                raise RuntimeError("翻訳エンジンの応答を解釈できませんでした。") from error

    def events(self, path, payload):
        with self._response(path, payload) as response:
            while line := response.readline(MAX_HTTP_BYTES + 1):
                self.check()
                if len(line) > MAX_HTTP_BYTES:
                    raise RuntimeError("翻訳エンジンの応答が大きすぎます。")
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise TypeError
                except (ValueError, TypeError, UnicodeError) as error:
                    raise RuntimeError("翻訳エンジンの応答を解釈できませんでした。") from error
                yield event
            self.check()


class RequestGroup:
    """One lightweight monitor for at most 32 request sockets."""

    def __init__(self):
        self._lock = threading.Lock()
        self._controls = set()
        self._finished = threading.Event()
        self._thread = threading.Thread(
            target=self._monitor, name="plamo-llama-cancel", daemon=True
        )

    def add(self, control):
        with self._lock:
            self._controls.add(control)

    def remove(self, control):
        with self._lock:
            self._controls.discard(control)

    def _monitor(self):
        while not self._finished.wait(0.02):
            with self._lock:
                controls = list(self._controls)
            for control in controls:
                if control.cancelled.is_set():
                    control.cancel()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._finished.set()
        with self._lock:
            controls = list(self._controls)
        for control in controls:
            control.cancel()
        self._thread.join(timeout=1)


@dataclass
class PreparedCompletion:
    text: str
    payload: dict
    codec: object
    slot: int | None = None

    @property
    def cost(self):
        return len(self.payload["prompt"]) + self.payload["n_predict"]


def iter_completions(take, capacity, prepare, complete, make_request, cancelled, token_budget):
    """Emit independent completions in finish order, with bounded FCFS admission."""
    completed = queue.Queue()
    running = {}
    available_slots = deque(range(MAX_PARALLEL))
    waiting = None
    reserved = 0
    pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="plamo-llama-http")
    with RequestGroup() as group:
        try:
            while True:
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                while True:
                    try:
                        future = completed.get_nowait()
                    except queue.Empty:
                        break
                    work, prepared, control = running.pop(future)
                    reserved -= prepared.cost
                    available_slots.append(prepared.slot)
                    group.remove(control)
                    try:
                        result = future.result()
                    except (ValueError, InterruptedError) as error:
                        result = error
                    yield work, result
                if waiting is not None and waiting[0].cancelled.is_set():
                    work, _prepared, control = waiting
                    control.cancel()
                    group.remove(control)
                    waiting = None
                    yield work, InterruptedError("翻訳を停止しました。")
                    continue
                limit = min(MAX_PARALLEL, max(1, int(capacity())))
                if waiting is None and len(running) < limit:
                    works = take(1)
                    if len(works) > 1:
                        raise RuntimeError("翻訳キューの同時処理数が一致しません。")
                    if works:
                        work = works[0]
                        if work.cancelled.is_set():
                            yield work, InterruptedError("翻訳を停止しました。")
                            continue
                        control = make_request(work)
                        group.add(control)
                        try:
                            prepared = prepare(work, control)
                            if not 0 < prepared.cost <= token_budget:
                                raise ValueError("段落が長すぎます。短く区切って翻訳してください。")
                        except (ValueError, InterruptedError) as error:
                            control.cancel()
                            group.remove(control)
                            yield work, error
                            continue
                        waiting = work, prepared, control
                if waiting is not None:
                    work, prepared, control = waiting
                    if len(running) < limit and reserved + prepared.cost <= token_budget:
                        # Do not skip an older large request to admit later small
                        # ones: the first queued reservation waits for capacity.
                        prepared.slot = available_slots.popleft()
                        prepared.payload["id_slot"] = prepared.slot
                        future = pool.submit(complete, work, prepared, control)
                        running[future] = waiting
                        reserved += prepared.cost
                        waiting = None
                        future.add_done_callback(completed.put)
                        continue
                if not running:
                    return
                # Polling also picks up newly submitted work while existing
                # requests are decoding. No accumulation/whole-batch barrier.
                cancelled.wait(0.01)
        finally:
            # Group shutdown closes each owned socket before joining workers;
            # no worker can outlive the model/lifecycle lock held by our caller.
            if waiting is not None:
                waiting[2].cancel()
            for _work, _prepared, control in running.values():
                control.cancel()
            pool.shutdown(wait=True, cancel_futures=True)

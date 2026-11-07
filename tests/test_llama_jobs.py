"""Bounded native HTTP workers, without Windows, models, or GPU initialization."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from plamo_cp import llama_jobs as jobs
from plamo_cp.engine import TranslationWork


def eventually(check):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.005)
    raise AssertionError("The expected bounded worker transition did not occur")


class Control:
    def __init__(self, work):
        self.cancelled = work.cancelled
        self.aborted = threading.Event()

    def cancel(self):
        self.aborted.set()


def test_shared_kv_reservations_keep_fcfs_and_refill_in_completion_order():
    works = [TranslationWork(name, name, {}) for name in ("a", "b", "c", "d")]
    costs = {"a": 6, "b": 6, "c": 2, "d": 2}
    released = {work.key: threading.Event() for work in works}
    started, admitted, results = [], [], queue.Queue()
    controls = {}

    def take(count):
        assert count == 1
        if not works:
            return []
        work = works.pop(0)
        admitted.append(work.key)
        return [work]

    def make_request(work):
        controls[work.key] = Control(work)
        return controls[work.key]

    def complete(work, _prepared, control):
        started.append(work.key)
        while not released[work.key].wait(0.005):
            if control.aborted.is_set():
                raise InterruptedError
        return work.key + " translated"

    def drain():
        for work, result in jobs.iter_completions(
            take,
            lambda: 32,
            lambda work, _control: jobs.PreparedCompletion(
                work.text, {"prompt": [1], "n_predict": costs[work.key] - 1}, None
            ),
            complete,
            make_request,
            threading.Event(),
            10,
        ):
            results.put((work.key, result))

    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(drain)
        try:
            eventually(lambda: admitted == ["a", "b"])
            assert started == ["a"]  # Large b prevents small c from overtaking.
            released["a"].set()
            assert results.get(timeout=1) == ("a", "a translated")
            eventually(lambda: set(started) == {"a", "b", "c", "d"})
            released["c"].set()
            assert results.get(timeout=1) == ("c", "c translated")
            released["b"].set()
            released["d"].set()
            future.result(timeout=1)
            assert {results.get(timeout=1)[0], results.get(timeout=1)[0]} == {"b", "d"}
        finally:
            for event in released.values():
                event.set()


def test_cancelled_weighted_head_releases_its_reservation_without_cancelling_other_work():
    first, second, third = (TranslationWork(name, name, {}) for name in ("a", "b", "c"))
    works = [first, second, third]
    controls, started, results = {}, [], queue.Queue()
    release = threading.Event()

    def make(work):
        controls[work.key] = Control(work)
        return controls[work.key]

    def complete(work, _prepared, control):
        started.append(work.key)
        while not release.wait(0.005):
            if control.aborted.is_set():
                raise InterruptedError
        return work.text

    def drain():
        for work, result in jobs.iter_completions(
            lambda _: [works.pop(0)] if works else [],
            lambda: 32,
            lambda work, _: jobs.PreparedCompletion(
                work.text, {"prompt": [1], "n_predict": 5 if work is second else 2}, None
            ),
            complete,
            make,
            threading.Event(),
            8,
        ):
            results.put((work.key, result))

    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(drain)
        try:
            eventually(lambda: "b" in controls)
            assert started == ["a"]
            second.cancelled.set()
            key, result = results.get(timeout=1)
            assert key == "b" and isinstance(result, InterruptedError)
            eventually(lambda: started == ["a", "c"])
            assert not controls["a"].aborted.is_set()
            release.set()
            future.result(timeout=1)
        finally:
            release.set()


def test_slot_cap_is_32_even_when_caller_requests_more_and_refills_without_a_barrier():
    works = [TranslationWork(index, str(index), {}) for index in range(33)]
    release = {work.key: threading.Event() for work in works}
    controls, started, results = {}, [], queue.Queue()

    def make(work):
        controls[work.key] = Control(work)
        return controls[work.key]

    def complete(work, _, control):
        started.append(work.key)
        while not release[work.key].wait(0.005):
            if control.aborted.is_set():
                raise InterruptedError
        return work.text

    def drain():
        for work, result in jobs.iter_completions(
            lambda _: [works.pop(0)] if works else [],
            lambda: 100,
            lambda work, _: jobs.PreparedCompletion(
                work.text, {"prompt": [1], "n_predict": 1}, None
            ),
            complete,
            make,
            threading.Event(),
            100,
        ):
            results.put((work.key, result))

    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(drain)
        try:
            eventually(lambda: len(started) == 32)
            assert 32 not in controls
            release[7].set()
            assert results.get(timeout=1) == (7, "7")
            eventually(lambda: 32 in started)
        finally:
            for event in release.values():
                event.set()
            future.result(timeout=2)


def test_empty_work_never_creates_a_request_and_bad_cost_does_not_block_next_work():
    forbidden = lambda *_: pytest.fail("Empty work must not touch a model or HTTP")
    assert (
        list(
            jobs.iter_completions(
                lambda _: [], lambda: 32, forbidden, forbidden, forbidden, threading.Event(), 8
            )
        )
        == []
    )
    works = [TranslationWork("bad", "bad", {}), TranslationWork("good", "good", {})]
    controls = []

    def make(work):
        control = Control(work)
        controls.append(control)
        return control

    result = list(
        jobs.iter_completions(
            lambda _: [works.pop(0)] if works else [],
            lambda: 32,
            lambda work, _: jobs.PreparedCompletion(
                work.text, {"prompt": [1], "n_predict": 8 if work.key == "bad" else 1}, None
            ),
            lambda work, *_: work.text,
            make,
            threading.Event(),
            8,
        )
    )
    assert isinstance(result[0][1], ValueError) and result[1][1] == "good"
    assert controls[0].aborted.is_set()


@pytest.mark.parametrize("stage", ["tokenize", "headers", "first-token"])
@pytest.mark.parametrize("shutdown_wakes_reader", [True, False])
def test_socket_cancellation_interrupts_each_prefill_stage_without_waiting_for_server(
    stage, shutdown_wakes_reader, monkeypatch
):
    if not shutdown_wakes_reader:
        # Winsock need not wake a timed receive's select() on local shutdown.
        monkeypatch.setattr(socket.socket, "shutdown", lambda *_: None)
    entered, begin, finish = threading.Event(), threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            if stage == "headers":
                begin.wait(1)
            if stage != "tokenize":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
            finish.wait(2)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cancelled = threading.Event()
    control = jobs.RequestControl(f"http://127.0.0.1:{server.server_port}", {}, cancelled)
    try:
        with ThreadPoolExecutor(max_workers=1) as caller, jobs.RequestGroup() as group:
            group.add(control)
            future = caller.submit(
                control.post_json if stage == "tokenize" else lambda *_: list(control.events(*_)),
                "/tokenize" if stage == "tokenize" else "/completion",
                {"prompt": [1], "id_slot": 0},
            )
            assert entered.wait(1)
            cancelled.set()
            if stage == "headers":
                # Keep ownership while the server has not acknowledged that
                # this completion started. It ACKs before any prompt decode.
                eventually(control._aborted.is_set)
                assert not future.done()
                begin.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=0.5)
        assert not finish.is_set()  # Cancellation did not depend on a server reply.
    finally:
        begin.set()
        finish.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_private_connections_clear_socket_ownership_and_never_reuse_health_pool(monkeypatch):
    made = []

    class Socket:
        def __init__(self):
            self.shutdowns = 0

        def settimeout(self, _):
            pass

        def shutdown(self, _):
            self.shutdowns += 1

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 8765 and timeout == 2
            self.sock = Socket()
            made.append(self)

        def connect(self):
            pass

        def request(self, method, path, *, body, headers):
            assert method == "POST" and path == "/tokenize"
            assert "X-Conversation-Id" not in headers
            assert headers["Authorization"] == "Bearer private"
            assert json.loads(body) == {"content": "source"}

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: b'{"tokens":[1]}', close=lambda: None)

        def close(self):
            pass

    monkeypatch.setattr(jobs.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(jobs, "CancellableSocket", lambda stream, _cancelled: stream)
    first = jobs.RequestControl(
        "http://127.0.0.1:8765", {"authorization": "Bearer private"}, threading.Event()
    )
    second = jobs.RequestControl(
        "http://127.0.0.1:8765", {"authorization": "Bearer private"}, threading.Event()
    )
    for control in (first, second):
        assert control.post_json("/tokenize", {"content": "source"}) == {"tokens": [1]}
        assert control._connection is None and control._socket is None
    first.cancel()
    assert len(made) == 2 and all(connection.sock.shutdowns == 0 for connection in made)


@pytest.mark.parametrize(
    "url", ["https://127.0.0.1:1", "http://localhost:1", "http://example.com:1"]
)
def test_request_destination_is_only_the_private_numeric_loopback(url):
    with pytest.raises(ValueError):
        jobs.RequestControl(url, {}, threading.Event())


def test_native_reservation_waits_for_its_started_slot_to_be_idle(monkeypatch):
    control = jobs.RequestControl(
        "http://127.0.0.1:8765", {"authorization": "Bearer secret"}, threading.Event()
    )
    control._started, control._completion_sent, control._slot = True, True, 7
    observations = []

    class Observer:
        def __init__(self, url, headers, _cancelled):
            assert url == "http://127.0.0.1:8765" and headers["Authorization"] == "Bearer secret"

        @contextmanager
        def _response(self, path, payload, **options):
            assert path == "/slots" and payload is None and options["method"] == "GET"
            observations.append(len(observations) == 0)
            body = json.dumps([{"id": 7, "is_processing": observations[-1]}]).encode()
            yield SimpleNamespace(read=lambda _: body)

        def cancel(self):
            pass

    monkeypatch.setattr(jobs, "RequestControl", Observer)
    control.finish()
    assert observations == [True, False]


def test_initially_idle_slot_is_not_a_release_ack_for_an_unstarted_completion():
    control = jobs.RequestControl("http://127.0.0.1:8765", {}, threading.Event())
    control._completion_sent, control._slot = True, 3
    with pytest.raises(jobs.NativeReleaseError):
        control.finish()  # No GET /slots can safely acknowledge this queue state.


def test_before_header_cancel_retains_ownership_until_begin_or_fatal_timeout():
    socket = SimpleNamespace(shutdown=lambda _: calls.append("shutdown"))
    connection = SimpleNamespace(close=lambda: calls.append("close"))
    calls = []
    control = jobs.RequestControl("http://127.0.0.1:8765", {}, threading.Event())
    control._connection, control._socket = connection, socket
    control._completion_sent = True
    control.cancel()
    assert calls == [] and not control._unsafe_release
    control._started = True
    control.cancel()
    assert calls == ["shutdown", "close"]
    control._started = False
    control._cancel_at = time.monotonic() - jobs.RELEASE_TIMEOUT - 1
    control.cancel()
    assert control._unsafe_release
    with pytest.raises(jobs.NativeReleaseError):
        control.finish()

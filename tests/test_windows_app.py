from __future__ import annotations

import sys
from types import SimpleNamespace

from plamo_cp import windows_app


def test_backend_command_for_frozen_application(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Apps/PLaMo/PLaMo for 競プロ.exe")
    assert windows_app.backend_command() == ["C:/Apps/PLaMo/PLaMo for 競プロ.exe", "--server-child"]


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


def summary_payloads(*, loaded=None, available=True):
    return {
        "/api/health": {
            "app": "plamo-for-cp",
            "engine": {"loaded_model": loaded, "state": "idle"},
        },
        "/api/setup": {
            "application": "plamo-for-cp",
            "selected_model": "2b",
            "quiet_level": 2,
            "models": [{"id": "2b", "available": available}],
            "preparation": {"stage": "idle"},
            "download": {"stage": "idle"},
        },
    }


def present(payloads):
    return windows_app.settings_summary(payloads["/api/health"], payloads["/api/setup"])


def test_settings_summary_shows_the_loaded_model_and_saved_settings():
    payloads = summary_payloads(loaded="0.8b")
    payloads["/api/health"]["engine"]["state"] = "translating"
    payloads["/api/setup"]["download"] = {"stage": "downloading", "model": "8b"}
    assert present(payloads) == {
        "model": "800M（0.8B）",
        "utilization": "64%",
    }


def test_settings_summary_stays_name_and_utilization_only_before_loading_or_downloading():
    expected = {"model": "なし", "utilization": "64%"}
    assert present(summary_payloads()) == expected
    assert present(summary_payloads(available=False)) == expected
    payloads = summary_payloads()
    payloads["/api/setup"]["quiet_level"] = 1
    assert present(payloads)["utilization"] == "80%"
    payloads["/api/setup"]["quiet_level"] = None
    payloads["/api/setup"]["quiet_mode"] = True
    assert present(payloads)["utilization"] == "50%"


def test_only_the_loaded_model_is_shown_and_missing_utilization_is_not_invented():
    values = windows_app.settings_summary({"selected_model": "2b", "engine": {}}, {})
    assert values["model"] == "なし"
    assert values["utilization"] == "—"
    assert set(values) == {"model", "utilization"}
    for unknown in ("31b", "future-model", 2, True):
        values = windows_app.settings_summary(
            {"selected_model": "2b", "engine": {"loaded_model": unknown}},
            {"selected_model": "2b"},
        )
        assert values["model"] == "—"


def test_summary_poller_only_reads_health_and_setup(monkeypatch):
    payloads = summary_payloads()
    paths = []
    poller = windows_app.SettingsSummaryPoller()

    def read(path):
        paths.append(path)
        return payloads[path]

    monkeypatch.setattr(poller, "_read_json", read)
    assert poller.refresh()
    values, connected = poller.results.get(timeout=1)
    assert connected and values == present(payloads)
    assert paths == list(payloads)
    assert not poller.refresh(), "An immediate retry must not continuously poll the API"
    poller.close()


def test_summary_poller_does_not_continue_with_an_unrelated_service(monkeypatch):
    paths = []
    poller = windows_app.SettingsSummaryPoller()

    def read(path):
        paths.append(path)
        return {"app": "another-program"}

    monkeypatch.setattr(poller, "_read_json", read)
    assert poller.refresh()
    assert poller.results.get(timeout=1) == (None, False)
    assert paths == ["/api/health"]
    poller.close()


def test_summary_failure_retains_connection_without_publishing_partial_settings(monkeypatch):
    payloads = summary_payloads()
    poller = windows_app.SettingsSummaryPoller()

    def read(path):
        if path == "/api/setup":
            raise OSError("Temporarily unavailable")
        return payloads[path]

    monkeypatch.setattr(poller, "_read_json", read)
    assert poller.refresh()
    assert poller.results.get(timeout=1) == (None, True)
    poller.close()


def test_summary_poller_does_not_overlap_or_publish_after_close(monkeypatch):
    import queue
    import threading

    started, release = threading.Event(), threading.Event()
    payloads = summary_payloads()
    poller = windows_app.SettingsSummaryPoller()

    def read(path):
        if path == "/api/health":
            started.set()
            assert release.wait(timeout=1)
        return payloads[path]

    monkeypatch.setattr(poller, "_read_json", read)
    assert poller.refresh()
    assert started.wait(timeout=1)
    assert not poller.refresh()
    poller.close()
    release.set()
    assert not poller.refresh()
    try:
        poller.results.get(timeout=0.05)
    except queue.Empty:
        pass
    else:
        raise AssertionError("A closed window must not receive another snapshot")


def test_summary_transport_rejects_redirects_and_oversized_bodies(monkeypatch):
    import pytest

    calls = []
    response = SimpleNamespace(status=302, length=0, close=lambda: None)

    class Connection:
        sock = None

        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def connect(self):
            pass

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(windows_app.http.client, "HTTPConnection", Connection)
    poller = windows_app.SettingsSummaryPoller()
    with pytest.raises(ValueError):
        poller._read_json("/api/health")
    response.status = 200
    response.length = windows_app.HEALTH_MAX_BYTES + 1
    with pytest.raises(ValueError):
        poller._read_json("/api/setup")
    assert calls[0] == ("127.0.0.1", windows_app.DEFAULT_PORT, windows_app.HEALTH_TIMEOUT)
    assert all(item[0] in {"127.0.0.1", "GET"} for item in calls)
    poller.close()


def test_summary_transport_deadline_aborts_a_stalled_read(monkeypatch):
    import threading

    import pytest

    released = threading.Event()

    class Socket:
        def shutdown(self, _how):
            released.set()

    class Connection:
        sock = None

        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            self.sock = Socket()

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            def read(_maximum):
                assert released.wait(timeout=1)
                return b"{}"

            return SimpleNamespace(status=200, length=None, read=read, close=lambda: None)

        def close(self):
            pass

    monkeypatch.setattr(windows_app.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(windows_app, "HEALTH_TIMEOUT", 0.02)
    poller = windows_app.SettingsSummaryPoller()
    with pytest.raises(ValueError):
        poller._read_json("/api/health")
    assert released.is_set()
    poller.close()


def test_summary_cancel_interrupts_connection_close_body_without_stopping_another_client(
    monkeypatch,
):
    import http.client
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    monkeypatch.setattr(windows_app, "HEALTH_TIMEOUT", 5)
    gates = [threading.Event(), threading.Event()]
    started = [threading.Event(), threading.Event()]
    request_lock = threading.Lock()
    requested = []
    body = b'{"app":"plamo-for-cp","engine":{"state":"idle"}}'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with request_lock:
                index = len(requested)
                requested.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            started[index].set()
            if gates[index].wait(timeout=2):
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    poller = windows_app.SettingsSummaryPoller(port=server.server_port)
    external = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
    response = None
    try:
        assert poller.refresh()
        assert started[0].wait(timeout=1)
        deadline = time.monotonic() + 1
        while True:
            with poller._lock:
                connection = poller._connection
                reading_close_body = (
                    connection is not None
                    and connection.sock is None
                    and poller._active_socket is not None
                )
            if reading_close_body:
                break
            assert time.monotonic() < deadline
            time.sleep(0.005)
        external.request("GET", "/api/health")
        response = external.getresponse()
        assert started[1].wait(timeout=1)
        poller.close()
        deadline = time.monotonic() + 0.5
        while poller._running:
            assert time.monotonic() < deadline, "Close must abort the read before the 5s deadline"
            time.sleep(0.005)
        assert not gates[0].is_set()
        assert poller.results.empty()
        gates[1].set()
        assert response.read() == body, "The other client's connection must remain usable"
        assert requested == ["/api/health", "/api/health"]
    finally:
        poller.close()
        for gate in gates:
            gate.set()
        if response is not None:
            response.close()
        external.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_summary_transport_rejects_a_valid_json_prefix_of_a_truncated_declared_body():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = b'{"app":"plamo-for-cp"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + 50))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    poller = windows_app.SettingsSummaryPoller(port=server.server_port)
    try:
        with pytest.raises(ValueError, match="Incomplete settings response"):
            poller._read_json("/api/health")
    finally:
        poller.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plamo_cp import desktop


@pytest.fixture
def service():
    servers = []

    def start(handler):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    handler(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.daemon = True
        thread.start()
        servers.append((server, thread))
        return server.server_port

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def respond(handler, body, status=200, **headers):
    assert handler.path == "/api/health"
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"app": "plamo-for-cp", "status": "ok"}, "ours"),
        ({"app": "another-app"}, "other"),
        ({"status": "ok"}, "other"),
    ],
)
def test_service_identity(service, monkeypatch, payload, expected):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    port = service(lambda handler: respond(handler, json.dumps(payload).encode()))
    assert desktop.service_state(port) == expected


def test_unrelated_non_json_service(service):
    port = service(lambda handler: respond(handler, b"<html>another service</html>"))
    assert desktop.service_state(port) == "other"


def test_absent_service():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert desktop.service_state(port) == "unavailable"


def test_health_probe_never_follows_a_redirect(service):
    redirected = []
    target = service(lambda handler: redirected.append(handler.path))
    port = service(
        lambda handler: respond(handler, b"", 302, Location=f"http://127.0.0.1:{target}/api/health")
    )
    assert desktop.service_state(port) == "other"
    assert redirected == []


def test_health_response_is_bounded_even_if_a_valid_identity_precedes_padding(service, monkeypatch):
    monkeypatch.setattr(desktop, "HEALTH_MAX_BYTES", 64)
    port = service(lambda handler: respond(handler, b'{"app":"plamo-for-cp"}' + b" " * 100))
    assert desktop.service_state(port) == "other"


def test_health_timeout_is_absolute_for_a_slow_drip(service, monkeypatch):
    monkeypatch.setattr(desktop, "HEALTH_TIMEOUT", 0.08)

    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.03)

    port = service(drip)
    started = time.monotonic()
    assert desktop.service_state(port) == "other"
    assert time.monotonic() - started < 0.5


@pytest.fixture(scope="module")
def swift_probe(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Native Foundation probe requires macOS and Swift")
    from pathlib import Path

    directory = tmp_path_factory.mktemp("native-health-probe")
    main = directory / "ProbeMain.swift"
    main.write_text(
        """import Foundation
@main struct Main {
    static func main() {
        let semaphore = DispatchSemaphore(value: 0)
        let probe = HealthProbe(port: Int(CommandLine.arguments[1])!,
                                timeout: Double(CommandLine.arguments[2])!,
                                maxBytes: Int(CommandLine.arguments[3])!)
        probe.start { ours, occupied in
            print("\\(ours) \\(occupied)")
            semaphore.signal()
        }
        withExtendedLifetime(probe) {
            if semaphore.wait(timeout: .now() + 3) != .success { exit(2) }
        }
    }
}
"""
    )
    binary = directory / "probe"
    source = Path(__file__).resolve().parents[1] / "packaging/macos/HealthProbe.swift"
    subprocess.run(
        ["swiftc", "-swift-version", "5", str(source), str(main), "-o", str(binary)],
        check=True,
        capture_output=True,
        timeout=30,
    )

    def run(port, timeout=1, max_bytes=65536):
        output = subprocess.check_output(
            [str(binary), str(port), str(timeout), str(max_bytes)], text=True, timeout=5
        )
        return output.strip().split()

    return run


def test_native_health_probe_accepts_only_our_bounded_identity(service, swift_probe):
    port = service(lambda handler: respond(handler, b'{"app":"plamo-for-cp"}'))
    assert swift_probe(port) == ["true", "true"]


def test_native_health_probe_never_follows_redirects(service, swift_probe):
    redirected = []
    target = service(lambda handler: redirected.append(handler.path))
    port = service(
        lambda handler: respond(handler, b"", 302, Location=f"http://127.0.0.1:{target}/api/health")
    )
    assert swift_probe(port) == ["false", "true"]
    assert redirected == []


def test_native_health_probe_rejects_oversized_responses(service, swift_probe):
    port = service(lambda handler: respond(handler, b'{"app":"plamo-for-cp"}' + b" " * 100))
    assert swift_probe(port, max_bytes=64) == ["false", "true"]


def test_native_health_probe_has_an_absolute_resource_timeout(service, swift_probe):
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.03)

    port = service(drip)
    started = time.monotonic()
    assert swift_probe(port, timeout=0.08) == ["false", "true"]
    assert time.monotonic() - started < 1


def test_socket_binds_only_loopback_and_rejects_conflict():
    first = desktop.listening_socket(0)
    try:
        host, port = first.getsockname()
        assert host == "127.0.0.1"
        with pytest.raises(OSError):
            desktop.listening_socket(port)
    finally:
        first.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses exclusive address binding")
def test_server_can_restart_immediately_after_an_established_connection_closes():
    first = desktop.listening_socket(0)
    port = first.getsockname()[1]
    first.setblocking(True)
    with socket.create_connection(("127.0.0.1", port)) as client:
        connection, _address = first.accept()
        connection.close()
        assert client.recv(1) == b""
    first.close()
    restarted = desktop.listening_socket(port)
    restarted.close()


def test_environment_preserves_existing_download_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PLAMO_DOWNLOAD_MIBPS", "0.25")
    desktop.prepare_environment()
    assert desktop.os.environ["PLAMO_DOWNLOAD_MIBPS"] == "0.25"
    assert (tmp_path / ".cache/plamo-cp/numba").is_dir()


def test_desktop_disables_routine_http_access_logging(monkeypatch):
    import uvicorn

    from plamo_cp import api

    class Socket:
        def close(self):
            pass

    class Server:
        def __init__(self, config):
            assert config.access_log is False

        def run(self, sockets):
            assert len(sockets) == 1

    monkeypatch.setattr(desktop.sys, "argv", ["backend"])
    monkeypatch.setattr(desktop, "prepare_environment", lambda: None)
    monkeypatch.setattr(desktop, "service_state", lambda _port: "unavailable")
    monkeypatch.setattr(desktop, "listening_socket", lambda _port: Socket())
    monkeypatch.setattr(api, "create_app", lambda: object())
    monkeypatch.setattr(uvicorn, "Server", Server)
    assert desktop.main() == 0

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ["/api/health", "/api/setup"]


def settings_payload():
    return {
        "health": {
            "app": "plamo-for-cp",
            "selected_model": "0.8b",
            "engine": {"loaded_model": None, "state": "idle"},
        },
        "setup": {
            "application": "plamo-for-cp",
            "selected_model": "0.8b",
            "quiet_level": 2,
            "models": [
                {"id": "0.8b", "available": True},
                {"id": "2b", "available": True},
                {"id": "8b", "available": False},
            ],
        },
    }


@pytest.fixture(scope="module")
def native_summary(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Native desktop summary requires macOS and Swift")
    directory = tmp_path_factory.mktemp("native-desktop-summary")
    main = directory / "SummaryMain.swift"
    main.write_text(
        """import Foundation
func render(_ value: DesktopSettingsSnapshot?) -> Any {
    guard let value = value else { return NSNull() }
    return ["model": value.model, "utilization": value.utilization]
}
@main struct Main {
    static func main() {
        let arguments = CommandLine.arguments
        var result: [String: Any]
        if arguments[1] == "parse" {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            let object = (try! JSONSerialization.jsonObject(with: data)) as! [String: Any]
            let snapshot = DesktopSettingsSnapshot(
                health: object["health"] as? [String: Any] ?? [:],
                setup: object["setup"] as? [String: Any] ?? [:])
            result = ["snapshot": render(snapshot)]
        } else {
            let semaphore = DispatchSemaphore(value: 0)
            let lock = NSLock()
            var snapshot: DesktopSettingsSnapshot?
            var count = 0
            let request = DesktopSettingsRequest(
                port: Int(arguments[2])!, timeout: Double(arguments[3])!,
                maxBytes: Int(arguments[4])!)
            request.start { value in
                lock.lock()
                snapshot = value
                count += 1
                lock.unlock()
                semaphore.signal()
            }
            let cancelDelay = Double(arguments[5])!
            if cancelDelay >= 0 {
                DispatchQueue.global().asyncAfter(deadline: .now() + cancelDelay) {
                    request.cancel()
                }
            }
            withExtendedLifetime(request) {
                if semaphore.wait(timeout: .now() + 4) != .success { exit(2) }
                // Give an already received, subsequently cancelled response a
                // chance to arrive; it must not produce a second completion.
                Thread.sleep(forTimeInterval: 0.25)
            }
            lock.lock()
            result = ["snapshot": render(snapshot), "callbacks": count]
            lock.unlock()
        }
        let output = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(data: output, encoding: .utf8)!)
    }
}
"""
    )
    binary = directory / "summary"
    subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "5",
            str(ROOT / "packaging/macos/DesktopSettings.swift"),
            str(main),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=40,
    )

    def run(payload=None, *, port=None, timeout=1, max_bytes=65536, cancel_after=-1):
        command = [str(binary), "parse"]
        if port is not None:
            command = [
                str(binary),
                "request",
                str(port),
                str(timeout),
                str(max_bytes),
                str(cancel_after),
            ]
        output = subprocess.check_output(
            command,
            input=json.dumps(payload or {}).encode(),
            timeout=6,
            env=dict(os.environ, HTTP_PROXY="http://127.0.0.1:1", NO_PROXY=""),
        )
        return json.loads(output)

    return run


def test_summary_displays_no_loaded_model_and_actual_utilization(native_summary):
    snapshot = native_summary(settings_payload())["snapshot"]
    assert snapshot == {
        "model": "なし",
        "utilization": "64%",
    }


@pytest.mark.parametrize("model,name", [("0.8b", "800M（0.8B）"), ("2b", "2B"), ("8b", "8B")])
def test_summary_displays_the_loaded_model_independently_of_saved_selection(
    native_summary, model, name
):
    payload = settings_payload()
    payload["setup"]["selected_model"] = "2b" if model != "2b" else "8b"
    payload["health"]["engine"]["loaded_model"] = model
    assert native_summary(payload)["snapshot"]["model"] == name


def test_selection_and_availability_do_not_invent_a_loaded_model(native_summary):
    payload = settings_payload()
    payload["setup"]["selected_model"] = "8b"
    assert native_summary(payload)["snapshot"]["model"] == "なし"
    payload["setup"]["models"] = []
    assert native_summary(payload)["snapshot"]["model"] == "なし"
    payload["health"]["engine"]["loaded_model"] = "8b"
    assert native_summary(payload)["snapshot"]["model"] == "8B"
    for invalid in ("unregistered-model", "", True, 8):
        payload["health"]["engine"]["loaded_model"] = invalid
        assert native_summary(payload)["snapshot"]["model"] == "—"
    payload["health"]["engine"].pop("loaded_model")
    assert native_summary(payload)["snapshot"]["model"] == "なし"


@pytest.mark.parametrize("part,key", [("health", "app"), ("setup", "application")])
@pytest.mark.parametrize("identity", ["another-app", None, True])
def test_summary_never_accepts_an_unrelated_service(native_summary, part, key, identity):
    payload = settings_payload()
    payload[part][key] = identity
    assert native_summary(payload)["snapshot"] is None


def test_missing_settings_are_unknown_instead_of_suggesting_defaults(native_summary):
    payload = {
        "health": {"app": "plamo-for-cp"},
        "setup": {"application": "plamo-for-cp"},
    }
    assert native_summary(payload)["snapshot"] == {"model": "なし", "utilization": "—"}


@pytest.mark.parametrize("level,percent", list(enumerate([100, 80, 64, 50, 40, 32, 25, 20])))
def test_summary_uses_the_same_eight_utilization_presets(native_summary, level, percent):
    payload = settings_payload()
    payload["setup"]["quiet_level"] = level
    assert native_summary(payload)["snapshot"]["utilization"] == f"{percent}%"


@pytest.mark.parametrize("invalid", [True, False, -1, 8, 1.5, "2", None])
def test_invalid_utilization_does_not_become_a_numeric_preset(native_summary, invalid):
    payload = settings_payload()
    payload["setup"]["quiet_level"] = invalid
    assert native_summary(payload)["snapshot"]["utilization"] == "—"


@pytest.mark.parametrize("legacy,percent", [(True, "50%"), (False, "100%")])
def test_valid_legacy_utilization_remains_readable(native_summary, legacy, percent):
    payload = settings_payload()
    payload["setup"].pop("quiet_level")
    payload["setup"]["quiet_mode"] = legacy
    assert native_summary(payload)["snapshot"]["utilization"] == percent
    payload["setup"]["quiet_level"] = 2
    assert native_summary(payload)["snapshot"]["utilization"] == "64%"


@pytest.mark.parametrize("legacy", ["true", 1, None])
def test_invalid_legacy_utilization_is_unknown(native_summary, legacy):
    payload = settings_payload()
    payload["setup"].pop("quiet_level")
    payload["setup"]["quiet_mode"] = legacy
    assert native_summary(payload)["snapshot"]["utilization"] == "—"


@pytest.fixture
def summary_service():
    servers = []

    def start(handler):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path))
                try:
                    handler(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                requests.append((self.command, self.path))
                self.send_error(405)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.daemon = True
        thread.start()
        servers.append((server, thread))
        return server.server_port, requests

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def send(handler, body, status=200, declared_length=True, **headers):
    handler.send_response(status)
    if declared_length:
        handler.send_header("Content-Length", str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def respond_settings(handler):
    payload = settings_payload()
    route_payload = dict(zip(ROUTES, [payload[key] for key in ["health", "setup"]]))
    send(handler, json.dumps(route_payload[handler.path]).encode())


def test_request_reads_only_the_two_fixed_get_routes(summary_service, native_summary):
    port, requests = summary_service(respond_settings)
    result = native_summary(port=port)
    assert result["snapshot"] == native_summary(settings_payload())["snapshot"]
    assert result["callbacks"] == 1
    assert requests == [("GET", route) for route in ROUTES]


def test_request_never_follows_a_redirect(summary_service, native_summary):
    target_port, target_requests = summary_service(respond_settings)
    port, requests = summary_service(
        lambda handler: send(
            handler, b"", 302, Location=f"http://127.0.0.1:{target_port}/api/health"
        )
    )
    assert native_summary(port=port) == {"snapshot": None, "callbacks": 1}
    assert requests == [("GET", "/api/health")]
    assert target_requests == []


@pytest.mark.parametrize("declared_length", [True, False])
def test_request_bounds_bodies_even_without_content_length(
    summary_service, native_summary, declared_length
):
    body = b'{"app":"plamo-for-cp"}' + b" " * 512
    port, requests = summary_service(
        lambda handler: send(handler, body, declared_length=declared_length)
    )
    assert native_summary(port=port, max_bytes=128) == {"snapshot": None, "callbacks": 1}
    assert requests == [("GET", "/api/health")]


def test_request_stops_after_an_http_failure(summary_service, native_summary):
    def handler(request):
        if request.path == "/api/setup":
            send(request, b"unavailable", 503)
        else:
            respond_settings(request)

    port, requests = summary_service(handler)
    assert native_summary(port=port) == {"snapshot": None, "callbacks": 1}
    assert requests == [("GET", "/api/health"), ("GET", "/api/setup")]


def test_request_has_an_absolute_timeout_for_a_slow_drip(summary_service, native_summary):
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.03)

    port, requests = summary_service(drip)
    started = time.monotonic()
    assert native_summary(port=port, timeout=0.08) == {"snapshot": None, "callbacks": 1}
    assert time.monotonic() - started < 1
    assert requests == [("GET", "/api/health")]


def test_cancel_prevents_late_responses_from_continuing_the_cohort(summary_service, native_summary):
    def slow_first_response(handler):
        if handler.path == "/api/health":
            time.sleep(0.15)
        respond_settings(handler)

    port, requests = summary_service(slow_first_response)
    assert native_summary(port=port, cancel_after=0.03) == {"snapshot": None, "callbacks": 1}
    assert requests == [("GET", "/api/health")]

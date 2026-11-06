"""Browser boundaries are checked before reading bodies or mutating local state."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import BrowserBoundaryMiddleware, TranslationRequest, create_app
from plamo_cp.cache import TranslationCache
from plamo_cp.translation_jobs import TranslationScheduler


class IdleEngine:
    def __init__(self):
        self.status = {"state": "idle", "loaded_model": None}

    def translate(self, text, **options):
        return "日本語: " + text


@pytest.fixture
def client(tmp_path):
    app = create_app(IdleEngine(), tmp_path / "cache.sqlite3", tmp_path / "settings.json")
    with TestClient(app, base_url="http://127.0.0.1:8765") as value:
        yield value


@pytest.mark.parametrize(
    "origin",
    [
        "https://foreign.example",
        "null",
        "http://127.0.0.1:9999",
        "http://localhost:8765",
        "http://127.0.0.1:8765.foreign.example",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
        "http://127.0.0.1:8765?query",
    ],
)
def test_foreign_origin_cannot_mutate_even_with_the_public_client_header(client, origin):
    before = client.get("/api/cache").json()
    response = client.post(
        "/api/cache/policy",
        json={"max_kib": 256},
        headers={"Origin": origin, "X-Plamo-Client": "setup"},
    )
    assert response.status_code == 403
    assert client.get("/api/cache").json() == before
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://127.0.0.1:8765",
        "chrome-extension://abcdefghijklmnop",
        "moz-extension://1e422501-ec2d-4d22-95fb-4c5ee2a62fae",
        "safari-web-extension://extension-id",
    ],
)
def test_native_and_userscript_background_origins_remain_usable(client, origin):
    headers = {"X-Plamo-Client": "setup"}
    if origin is not None:
        headers["Origin"] = origin
    response = client.post("/api/cache/policy", json={"max_kib": 256}, headers=headers)
    assert response.status_code == 200
    assert response.json()["max_bytes"] == 256 * 1024


def test_gm_transport_can_forward_page_origin_on_only_its_exact_routes(client):
    headers = {"Origin": "https://codeforces.com", "X-Plamo-Client": "userscript"}
    request = {"segments": [{"id": "p", "text": "Find the answer."}]}
    assert client.get("/api/setup", headers=headers).status_code == 200
    assert client.get("/api/health", headers=headers).status_code == 200
    translated = client.post("/api/translate", json=request, headers=headers)
    assert translated.status_code == 200
    assert translated.json()["translations"] == [{"id": "p", "text": "日本語: Find the answer."}]
    started = client.post("/api/translation-jobs", json=request, headers=headers)
    assert started.status_code == 202
    identifier = started.json()["id"]
    assert client.get(f"/api/translation-jobs/{identifier}", headers=headers).status_code == 200
    assert (
        client.post(
            f"/api/translation-jobs/{identifier}/cancel", json={}, headers=headers
        ).status_code
        == 200
    )
    assert client.get("/api/cache", headers=headers).status_code == 403
    assert client.post("/api/cache/clear", json={}, headers=headers).status_code == 403
    assert (
        client.post(
            "/api/cache/clear", json={}, headers={**headers, "X-Plamo-Client": "setup"}
        ).status_code
        == 403
    )


def test_foreign_plain_fetch_and_custom_header_preflight_cannot_use_gm_exception(client):
    origin = {"Origin": "https://foreign.example"}
    assert client.get("/api/setup", headers=origin).status_code == 403
    assert client.post("/api/translate", json={}, headers=origin).status_code == 403
    preflight = client.options(
        "/api/translate",
        headers={
            **origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Plamo-Client, Content-Type",
        },
    )
    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("path", ["/", "/text", "/setup.js", "/api/health", "/missing"])
def test_management_and_error_pages_cannot_be_framed_or_type_sniffed(client, path):
    response = client.get(path)
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    if path.startswith("/api/"):
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host", ["foreign.example", "testserver", "127.0.0.1.foreign.example"])
def test_production_host_allowlist_has_no_test_only_dns_rebinding_exception(client, host):
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-plamo-client", b"setup"), (b"origin", b"https://foreign.example")],
    ],
)
def test_unauthorized_write_is_rejected_without_reading_any_body(headers):
    async def check():
        messages = []

        async def downstream(*args):
            raise AssertionError("Unauthenticated request reached the application")

        async def receive():
            raise AssertionError("Unauthenticated body was read")

        async def send(message):
            messages.append(message)

        await BrowserBoundaryMiddleware(downstream)(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": "/api/models/folder",
                "headers": [(b"host", b"127.0.0.1:8765"), *headers],
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 403

    asyncio.run(check())


@pytest.mark.parametrize("content_length", [None, "256001", "9" * 5000, "not-a-number"])
def test_management_body_limits_cover_chunked_and_hostile_content_length(client, content_length):
    headers = {"X-Plamo-Client": "setup", "Content-Type": "application/json"}
    if content_length is not None:
        headers["Content-Length"] = content_length
    body = iter([b" " * 128000, b" " * 128001])
    response = client.post("/api/cache/policy", content=body, headers=headers)
    assert response.status_code == 413
    assert client.get("/api/cache").json()["max_bytes"] == 1024 * 1024


def test_repeated_cancelled_jobs_do_not_accumulate_unbounded_ready_queue(tmp_path):
    class HeldExecutor:
        def submit(self, operation):
            # Simulate a worker occupied by a model operation. It cannot drain
            # cancelled IDs while another browser continually submits work.
            pass

    cache = TranslationCache(tmp_path / "cache.sqlite3")
    scheduler = TranslationScheduler(
        IdleEngine(),
        cache,
        HeldExecutor(),
        lambda payload, segment: segment.id,
        lambda payload: {},
        lambda source, translated: None,
        max_jobs=1,
        max_segments=1,
    )
    request = TranslationRequest(segments=[{"id": "p", "text": "Find the answer."}])
    try:
        for _ in range(1000):
            job = scheduler.submit(request)
            assert len(scheduler._ready) == 1
            scheduler.cancel(job.id)
            assert len(scheduler._ready) == 0
            assert scheduler._pending == 0
        assert len(scheduler._jobs) == 1
    finally:
        scheduler.close()
        cache.close()

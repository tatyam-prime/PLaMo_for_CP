"""The plain-text page is served locally; removed PDF processing routes stay absent."""

from fastapi.testclient import TestClient

from plamo_cp import api


class FakeEngine:
    def __init__(self):
        self.status = {"state": "idle", "loaded_model": None}


def test_text_assets_and_legacy_bookmark_redirect(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "text.html").write_text("<!doctype html><p>原文と訳文</p>", encoding="utf-8")
    (assets / "text.js").write_text("window.textTranslationReady = true;", encoding="utf-8")
    monkeypatch.setattr(api, "SETUP_ASSETS", assets)
    app = api.create_app(
        engine=FakeEngine(),
        cache_path=tmp_path / "translations.sqlite3",
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/text")
        assert page.status_code == 200 and "原文と訳文" in page.text
        assert page.headers["content-type"].startswith("text/html")
        script = client.get("/text.js")
        assert script.status_code == 200 and script.text == "window.textTranslationReady = true;"
        assert script.headers["content-type"].startswith("application/javascript")
        legacy = client.get("/pdf", follow_redirects=False)
        assert legacy.status_code == 307 and legacy.headers["location"] == "/text"
        assert client.get("/pdf").text == page.text


def test_pdf_assets_and_processing_routes_are_retired_without_accepting_remote_urls(tmp_path):
    app = api.create_app(
        engine=FakeEngine(),
        cache_path=tmp_path / "translations.sqlite3",
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/pdf.js").status_code == 404
        assert (
            client.post(
                "/api/pdf/fetch",
                json={"url": "https://example.com/private.pdf"},
                headers={"X-Plamo-Client": "setup"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/pdf/extract",
                content=b"%PDF-1.7\nretired input",
                headers={"X-Plamo-Client": "setup", "Content-Type": "application/pdf"},
            ).status_code
            == 404
        )
        assert not any(path.startswith("/api/pdf/") for path in app.openapi()["paths"])

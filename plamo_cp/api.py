"""Loopback-only API for the Tampermonkey script and desktop setup."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .cache import RETENTION_DAYS, TranslationCache
from .engine import (
    DEFAULT_MODEL_ID,
    MODEL_SPECS,
    PROMPT_VERSION,
    SUPPORTED_MODEL_IDS,
    check_placeholders,
)
from .model_storage import ModelStorage
from .performance import LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL
from .runtime import make_engine
from .setup import SetupController
from .translation_jobs import JobQueueFull, TranslationScheduler

logger = logging.getLogger(__name__)
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "plamo-cp" / "translations.sqlite3"
SETUP_ASSETS = Path(__file__).resolve().parent / "assets"
USER_SCRIPT_DIST = Path(__file__).resolve().parent.parent / "dist" / "plamo-for-cp.user.js"


def _userscript_path() -> Path | None:
    """Select the same fixed source or bundled script for install and health."""
    for path in (USER_SCRIPT_DIST, SETUP_ASSETS / "plamo-for-cp.user.js"):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _userscript_version() -> str | None:
    path = _userscript_path()
    if path is None:
        return None
    try:
        with path.open("rb") as script:
            # Do not read the bundled application body on each health probe.
            # Replacement decoding also tolerates a UTF-8 codepoint cut at the
            # bounded read's end, after an otherwise complete metadata block.
            lines = script.read(8192).decode("utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "// ==UserScript==":
        return None
    versions = []
    for line in lines[1:]:
        if line.strip() == "// ==/UserScript==":
            return versions[0] if len(versions) == 1 else None
        if not line.lstrip().startswith("//"):
            return None
        if re.match(r"\s*//\s*@version(?:\s|$)", line):
            match = re.fullmatch(r"\s*//\s*@version[ \t]+([0-9][A-Za-z0-9._+-]{0,127})[ \t]*", line)
            if match is None:
                return None
            versions.append(match[1])
    return None


class BrowserBoundaryMiddleware:
    """Reject browser-cross-site mutations before parsing an untrusted body."""

    SETUP_ACTIONS = frozenset(
        {
            "/api/models/choose-folder",
            "/api/models/folder",
            "/api/models/open-folder",
            "/api/models/delete",
            "/api/models/unload",
            "/api/models/download",
            "/api/models/download/cancel",
            "/api/setup/prepare",
            "/api/cache/clear",
            "/api/cache/policy",
            "/api/performance",
        }
    )

    def __init__(self, app):
        self.app = app

    @staticmethod
    def userscript_route(path: str, method: str) -> bool:
        if method == "GET" and path in {"/api/setup", "/api/health"}:
            return True
        if method == "POST" and path in {"/api/translate", "/api/translation-jobs"}:
            return True
        suffix = "/cancel" if method == "POST" else "" if method == "GET" else None
        return (
            suffix is not None
            and re.fullmatch(
                r"/api/translation-jobs/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
                + suffix,
                path,
            )
            is not None
        )

    @staticmethod
    def allowed_origin(origin: str, host: str, scheme: str) -> bool:
        try:
            value = urlsplit(origin)
            if (
                value.username is not None
                or value.password is not None
                or value.path not in {"", "/"}
                or value.query
                or value.fragment
            ):
                return False
            if value.scheme in {"chrome-extension", "moz-extension", "safari-web-extension"}:
                return bool(value.hostname) and value.port is None
            target = urlsplit("//" + host)
            return (
                value.scheme == scheme == "http"
                and value.hostname in {"127.0.0.1", "localhost"}
                and value.hostname == target.hostname
                and (value.port or 80) == (target.port or 80)
            )
        except (ValueError, UnicodeError):
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "")
        headers = dict(scope.get("headers", []))
        userscript_route = self.userscript_route(path, method)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Content-Security-Policy"] = "frame-ancestors 'none'"
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "no-referrer"
                if path.startswith("/api/"):
                    response_headers["Cache-Control"] = "no-store"
            await send(message)

        if path.startswith("/api/"):
            origin = headers.get(b"origin")
            # Some GM transports may preserve the page Origin. Their fixed,
            # nonsafelisted header is accepted only on these narrow routes.
            # Web fetch cannot set it without a preflight, which remains denied.
            gm_transport = userscript_route and headers.get(b"x-plamo-client") == b"userscript"
            if (
                origin is not None
                and not gm_transport
                and not self.allowed_origin(
                    origin.decode("latin-1"),
                    headers.get(b"host", b"").decode("latin-1"),
                    scope.get("scheme", "http"),
                )
            ):
                return await JSONResponse(
                    {"detail": "アプリまたは翻訳スクリプトから操作してください。"}, status_code=403
                )(scope, receive, secure_send)
            if method == "POST":
                expected = None
                if path in self.SETUP_ACTIONS:
                    expected = b"setup"
                elif userscript_route:
                    expected = b"userscript"
                if expected is not None and headers.get(b"x-plamo-client") != expected:
                    return await JSONResponse(
                        {"detail": "アプリまたは翻訳スクリプトから操作してください。"},
                        status_code=403,
                    )(scope, receive, secure_send)
        await self.app(scope, receive, secure_send)


class RequestSizeLimitMiddleware:
    """Bound JSON before parsing while preserving the ASGI disconnect channel."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope.get("path", "").startswith("/api/")
            or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            return await self.app(scope, receive, send)
        max_bytes = 256000
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        normalized_length = (content_length.lstrip(b"0") or b"0") if content_length else b"0"
        if content_length and (
            not content_length.isdigit()
            or len(normalized_length) > 6
            or int(normalized_length) > max_bytes
        ):
            return await HTMLResponse("Request body too large.", status_code=413)(
                scope, receive, send
            )
        size = 0
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > max_bytes:
                return await HTMLResponse("Request body too large.", status_code=413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def receive_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, receive_body, send)


class Segment(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8000)


class HistoryPair(BaseModel):
    source: str = Field(min_length=1, max_length=6000)
    translation: str = Field(min_length=1, max_length=6000)


class GlossaryTerm(BaseModel):
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=256)


class TranslationRequest(BaseModel):
    model: Literal["0.8b", "2b", "8b"] = DEFAULT_MODEL_ID
    precision: Literal["4bit"] = "4bit"
    document_type: Literal["problem", "general"] = "problem"
    source_context: str = Field(default="", max_length=6000)
    batch_size: int = Field(default=1, ge=1, le=32, strict=True)
    history: list[HistoryPair] = Field(default_factory=list, max_length=3)
    glossary: list[GlossaryTerm] | None = Field(default=None, max_length=64)
    source_lang: str = Field(
        default="English", min_length=2, max_length=32, pattern=r"^[A-Za-z][A-Za-z -]+$"
    )
    target_lang: str = Field(
        default="Japanese", min_length=2, max_length=32, pattern=r"^[A-Za-z][A-Za-z -]+$"
    )
    segments: list[Segment] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_segments(self):
        if "<|plamo:" in self.source_context.casefold():
            raise ValueError("周辺の英文にモデルの制御トークンを含めることはできません。")
        if len({segment.id for segment in self.segments}) != len(self.segments):
            raise ValueError("Segment IDs must be unique.")
        if sum(len(segment.text) for segment in self.segments) > 32000:
            raise ValueError(
                "Request is too large; split it into smaller batches (32000 characters maximum)."
            )
        if sum(len(pair.source) + len(pair.translation) for pair in self.history) > 6000:
            raise ValueError("翻訳履歴は合計6000文字以内にしてください。")
        for pair in self.history:
            if "<|plamo:" in pair.source or "<|plamo:" in pair.translation:
                raise ValueError("翻訳履歴に制御トークンを含めることはできません。")
            check_placeholders(pair.source, pair.translation)
        if sum(len(term.source) + len(term.target) for term in (self.glossary or [])) > 6000:
            raise ValueError("用語集は合計6000文字以内にしてください。")
        for term in self.glossary or []:
            if not term.source.strip() or not term.target.strip():
                raise ValueError("用語集に空の語句は指定できません。")
            if any(
                marker in value
                for value in (term.source, term.target)
                for marker in ("<|plamo:", "[[PLM_", "<plm-", "</plm-")
            ):
                raise ValueError("用語集に数式や書式の制御記号は指定できません。")
        return self


class TranslatedSegment(BaseModel):
    id: str
    text: str


class SegmentError(BaseModel):
    id: str
    message: str


class TranslationResponse(BaseModel):
    translations: list[TranslatedSegment]
    model: str
    precision: str
    cached: int
    errors: list[SegmentError] | None = None


class PrepareRequest(BaseModel):
    model: Literal["0.8b", "2b", "8b"]


class StoredModelRequest(BaseModel):
    model: Literal["0.8b", "2b", "8b", "31b"]


class CachePolicyRequest(BaseModel):
    retention_days: Literal[7] = 7
    max_kib: Literal[256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536] | None = None
    max_mib: int | None = Field(default=None, ge=1, le=64, strict=True)

    @model_validator(mode="after")
    def validate_capacity(self):
        if (self.max_kib is None) == (self.max_mib is None):
            raise ValueError("保存容量は max_kib または max_mib のどちらか一つで指定してください。")
        return self

    @property
    def capacity_bytes(self) -> int:
        return self.max_kib * 1024 if self.max_kib is not None else self.max_mib * 1024 * 1024


class ModelFolderRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=4096)


class PerformanceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    quiet_level: int | None = Field(default=None, ge=0, le=MAX_QUIET_LEVEL, strict=True)
    quiet_mode: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def validate_setting(self):
        if len(self.model_fields_set) != 1 or (
            self.quiet_level is None and self.quiet_mode is None
        ):
            raise ValueError("quiet_level または quiet_mode のどちらか一つを指定してください。")
        return self

    @property
    def level(self) -> int:
        return (
            self.quiet_level
            if self.quiet_level is not None
            else LEGACY_QUIET_LEVEL
            if self.quiet_mode
            else 0
        )


def _verify_translation(source: str, translated: str) -> None:
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError(
            "The model returned an empty translation. The original paragraph was kept."
        )
    check_placeholders(source, translated)


def _public_model_specs(specs=None) -> list[dict]:
    models = []
    for model_id, spec in (specs or MODEL_SPECS).items():
        if model_id not in SUPPORTED_MODEL_IDS:
            continue
        if is_dataclass(spec):
            item = asdict(spec)
        elif isinstance(spec, dict):
            item = dict(spec)
        else:
            item = vars(spec).copy()
        item.setdefault("id", model_id)
        item.setdefault("name", f"PLaMo 3 Translate {model_id.upper()}")
        models.append(item)
    return models


def create_app(
    engine=None, cache_path: str | Path | None = None, settings_path=None, model_storage=None
) -> FastAPI:
    engine = engine if engine is not None else make_engine()
    cache = TranslationCache(
        cache_path if cache_path is not None else DEFAULT_CACHE_PATH,
        retention_days=RETENTION_DAYS,
    )
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plamo-inference")
    setup = SetupController(engine, executor, settings_path=settings_path)
    storage = (
        model_storage
        if model_storage is not None
        else ModelStorage(engine, settings_path=settings_path)
    )
    model_operation = False
    translations_pending = 0

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()
        setup.cancel()
        await asyncio.to_thread(setup.close)
        # Wait for the cancelled generation to leave its worker before closing SQLite.
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        close = getattr(engine, "close", None)
        if close:
            await asyncio.to_thread(close)
        cache.close()

    app = FastAPI(title="PLaMo for 競プロ", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_middleware(BrowserBoundaryMiddleware)
    app.state.engine = engine
    app.state.cache = cache
    app.state.setup = setup
    app.state.model_storage = storage

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "app": "plamo-for-cp",
            "application": "plamo-for-cp",
            "version": __version__,
            "selected_model": setup.selected_model,
            "userscript_version": await asyncio.to_thread(_userscript_version),
            "engine": engine.status,
        }

    @app.get("/api/models")
    async def models():
        return {
            "models": _public_model_specs(setup.model_specs),
            "precisions": ["4bit"],
            "default_model": setup.selected_model,
        }

    @app.get("/api/models/storage")
    async def model_storage_status():
        return await asyncio.to_thread(storage.snapshot)

    async def run_model_operation(operation, busy_message, *, block_download=True):
        nonlocal model_operation
        if (
            model_operation
            or translations_pending
            or jobs.busy
            or setup.preparing
            or (block_download and setup.downloading)
            or engine.status.get("state", "idle") != "idle"
        ):
            raise HTTPException(409, busy_message)
        model_operation = True
        future = None

        def release(value):
            nonlocal model_operation
            model_operation = False
            if not value.cancelled():
                value.exception()

        try:
            future = asyncio.get_running_loop().run_in_executor(executor, operation)
            return await asyncio.shield(future)
        finally:
            # Closing the settings page must not release a still-open native
            # picker or a running file operation to overlap another model task.
            if future is not None and not future.done():
                future.add_done_callback(release)
            else:
                model_operation = False

    async def change_model_folder(operation):
        def change():
            result = operation()
            if not result.get("cancelled"):
                setup.reset_preparation()
            return result

        try:
            return await run_model_operation(
                change, "翻訳やモデルの準備が終わってから保存先を変更してください。"
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except (OSError, RuntimeError) as error:
            raise HTTPException(
                503, str(error) or "保存先を変更できませんでした。もう一度お試しください。"
            ) from error

    @app.post("/api/models/choose-folder")
    async def choose_model_folder(x_plamo_client: Annotated[str | None, Header()] = None):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        return await change_model_folder(storage.choose_folder)

    @app.post("/api/models/folder")
    async def set_model_folder(
        payload: ModelFolderRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        return await change_model_folder(lambda: storage.set_folder(payload.folder))

    @app.post("/api/models/open-folder")
    async def open_model_folder(x_plamo_client: Annotated[str | None, Header()] = None):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        try:
            await asyncio.to_thread(storage.open_folder)
        except (OSError, RuntimeError) as error:
            raise HTTPException(
                503, "保存フォルダーを開けませんでした。表示された場所を確認してください。"
            ) from error
        return {"opened": True}

    @app.post("/api/models/delete")
    async def delete_model(
        payload: StoredModelRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")

        def remove():
            result = storage.delete(payload.model)
            setup.forget_model(payload.model)
            return result

        try:
            return await run_model_operation(
                remove, "翻訳やモデルの準備が終わってから削除してください。"
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except (OSError, RuntimeError) as error:
            raise HTTPException(
                503,
                "モデルを削除できませんでした。ほかのアプリで使用中の場合は、終了してからお試しください。",
            ) from error

    @app.post("/api/models/unload")
    async def unload_model(
        payload: StoredModelRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        if payload.model not in getattr(storage, "model_specs", MODEL_SPECS):
            raise HTTPException(422, "モデルを選択してください。")

        def unload():
            engine.unload(payload.model)
            setup.forget_model(payload.model)
            return {"unloaded": True}

        try:
            return await run_model_operation(
                unload,
                "翻訳やモデルの準備が終わってからメモリを解放してください。",
                block_download=False,
            )
        except (OSError, RuntimeError) as error:
            raise HTTPException(503, "モデルのメモリを解放できませんでした。") from error

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((SETUP_ASSETS / "setup.html").read_text(encoding="utf-8"))

    @app.get("/setup.js")
    async def setup_script():
        return FileResponse(SETUP_ASSETS / "setup.js", media_type="application/javascript")

    @app.get("/text", response_class=HTMLResponse)
    async def text_view():
        return HTMLResponse((SETUP_ASSETS / "text.html").read_text(encoding="utf-8"))

    @app.get("/text.js")
    async def text_script():
        return FileResponse(SETUP_ASSETS / "text.js", media_type="application/javascript")

    @app.get("/pdf")
    async def legacy_pdf_view():
        return RedirectResponse("/text", status_code=307)

    @app.get("/translation-jobs.js")
    async def translation_jobs_script():
        return FileResponse(
            SETUP_ASSETS / "translation-jobs.js", media_type="application/javascript"
        )

    @app.get("/userscript-version.js")
    async def userscript_version_script():
        return FileResponse(
            SETUP_ASSETS / "userscript-version.js", media_type="application/javascript"
        )

    @app.get("/licenses/plamo-community-license-{language}.txt")
    async def model_license(language: str):
        if language not in {"ja", "en"}:
            raise HTTPException(404, "ライセンス文書が見つかりません。")
        return FileResponse(
            SETUP_ASSETS / "licenses" / f"plamo-community-license-{language}.txt",
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/install.user.js")
    async def browser_userscript():
        path = _userscript_path()
        if path is None:
            raise HTTPException(404, "翻訳スクリプトの配布ファイルを準備中です。")
        return FileResponse(
            path,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/setup")
    async def setup_status():
        return await asyncio.to_thread(setup.snapshot)

    @app.post("/api/performance")
    async def set_performance(
        payload: PerformanceRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        try:
            # Do not queue this behind model work: the runtime switch is safe
            # during generation and must affect the currently running worker.
            return await asyncio.to_thread(setup.performance.set_quiet_level, payload.level)
        except (OSError, RuntimeError) as error:
            raise HTTPException(
                503, str(error) or "推論の稼働率を変更できませんでした。"
            ) from error

    @app.get("/api/cache")
    async def cache_status():
        return await asyncio.to_thread(cache.stats)

    @app.post("/api/cache/clear")
    async def clear_cache(x_plamo_client: Annotated[str | None, Header()] = None):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        cleared = await asyncio.to_thread(cache.clear)
        return {"cleared": cleared, "entries": 0, "bytes": 0}

    @app.post("/api/cache/policy")
    async def set_cache_policy(
        payload: CachePolicyRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        return await asyncio.to_thread(
            cache.set_policy,
            retention_days=RETENTION_DAYS,
            max_bytes=payload.capacity_bytes,
        )

    @app.post("/api/models/download", status_code=202)
    async def download_model_files(
        payload: PrepareRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        if model_operation:
            raise HTTPException(409, "保存先の変更やファイル操作が終わるまでお待ちください。")
        try:
            return setup.start_download(payload.model)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/models/download/cancel")
    async def cancel_model_download(x_plamo_client: Annotated[str | None, Header()] = None):
        if x_plamo_client != "setup":
            raise HTTPException(403, "設定・管理画面から操作してください。")
        return setup.cancel_download()

    @app.post("/api/setup/prepare", status_code=202)
    async def prepare_model(
        payload: PrepareRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "setup":
            raise HTTPException(403, "初回設定画面から操作してください。")
        if model_operation or translations_pending or jobs.busy or setup.preparing:
            raise HTTPException(409, "翻訳や保存先の変更が終わるまでお待ちください。")
        if not await asyncio.to_thread(setup.model_available, payload.model):
            raise HTTPException(409, "先にモデルをダウンロードしてください。")
        # Cache inspection yields to the loop. A translation or file operation
        # may have started meanwhile; do not queue a model switch behind it.
        if model_operation or translations_pending or jobs.busy or setup.preparing:
            raise HTTPException(409, "翻訳や保存先の変更が終わるまでお待ちください。")
        try:
            return setup.start(payload.model)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    def segment_key(payload: TranslationRequest, segment: Segment) -> str:
        key = cache.key(
            str(PROMPT_VERSION),
            payload.model,
            str(setup.model_specs[payload.model]["revision"]),
            str(engine.status.get("backend", "mlx")),
            payload.precision,
            payload.document_type,
            json.dumps(
                [pair.model_dump() for pair in payload.history],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                None
                if payload.glossary is None
                else [term.model_dump() for term in payload.glossary],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            payload.source_lang,
            payload.target_lang,
            segment.text,
        )
        if payload.source_context:
            # Empty context preserves existing cache keys and prompt bytes.
            key = cache.key("source-context-v1", key, payload.source_context)
        return key

    def translation_extra(payload: TranslationRequest) -> dict:
        extra = {"document_type": "general"} if payload.document_type == "general" else {}
        if payload.history:
            extra["history"] = [pair.model_dump() for pair in payload.history]
        if payload.glossary is not None:
            extra["glossary"] = [term.model_dump() for term in payload.glossary]
        if payload.source_context:
            extra["source_context"] = payload.source_context
        return extra

    jobs = TranslationScheduler(
        engine, cache, executor, segment_key, translation_extra, _verify_translation
    )
    app.state.translation_jobs = jobs

    async def submit_translation(payload, x_plamo_client, *, strict=False, lease=True):
        nonlocal translations_pending
        if x_plamo_client != "userscript":
            raise HTTPException(403, "PLaMo の Tampermonkey スクリプトから翻訳してください。")
        if payload.batch_size not in getattr(engine, "supported_batch_sizes", (1,)):
            raise HTTPException(
                422, "このパソコンでは選択した段落数を使用できません。1段落を選択してください。"
            )
        if model_operation:
            raise HTTPException(409, "保存先の変更が終わるまでお待ちください。")
        translations_pending += 1
        try:
            available = getattr(engine, "model_available", None)
            if setup.is_downloading(payload.model) or (
                available and not await asyncio.to_thread(available, payload.model)
            ):
                raise HTTPException(
                    409, "翻訳用データが未取得です。アプリの管理画面でダウンロードしてください。"
                )
            return jobs.submit(payload, strict=strict, lease=lease)
        except JobQueueFull as error:
            raise HTTPException(429, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        finally:
            translations_pending -= 1

    @app.post("/api/translation-jobs", status_code=202)
    async def start_translation_job(
        payload: TranslationRequest, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        job = await submit_translation(payload, x_plamo_client)
        return {"id": job.id, "state": "queued", "cursor": 0, "total": job.total}

    @app.get("/api/translation-jobs/{job_id}")
    async def translation_job_status(
        job_id: UUID,
        after: int = Query(default=0, ge=0, le=64),
        x_plamo_client: Annotated[str | None, Header()] = None,
    ):
        if x_plamo_client != "userscript":
            raise HTTPException(403, "翻訳スクリプトから操作してください。")
        try:
            return jobs.poll(str(job_id), after)
        except KeyError as error:
            raise HTTPException(
                404, "翻訳結果が見つかりません。もう一度翻訳してください。"
            ) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/translation-jobs/{job_id}/cancel")
    async def cancel_translation_job(
        job_id: UUID, x_plamo_client: Annotated[str | None, Header()] = None
    ):
        if x_plamo_client != "userscript":
            raise HTTPException(403, "翻訳スクリプトから操作してください。")
        try:
            return jobs.cancel(str(job_id))
        except KeyError as error:
            raise HTTPException(404, "翻訳処理が見つかりません。") from error

    @app.post(
        "/api/translate", response_model=TranslationResponse, response_model_exclude_none=True
    )
    async def translate(
        payload: TranslationRequest,
        request: Request,
        x_plamo_client: Annotated[str | None, Header()] = None,
    ):
        job = await submit_translation(
            payload, x_plamo_client, strict=payload.batch_size == 1, lease=False
        )

        async def watch_disconnect():
            while not await request.is_disconnected():
                await asyncio.sleep(0.1)
            jobs.cancel(job.id)

        disconnected = asyncio.create_task(watch_disconnect())
        waiting = asyncio.create_task(asyncio.to_thread(job.done.wait))
        try:
            done, _ = await asyncio.wait(
                [waiting, disconnected], return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done and not job.done.is_set():
                raise HTTPException(499, "ブラウザーとの接続が切れたため翻訳を停止しました。")
            if job.error:
                raise HTTPException(job.error["status"], job.error["message"])
            translated = {
                event["id"]: event["text"] for event in job.events if event["type"] == "translation"
            }
            errors = {
                event["id"]: event["message"]
                for event in job.events
                if event["type"] == "segment_error"
            }
            result = {
                "translations": [
                    {"id": segment.id, "text": translated[segment.id]}
                    for segment in payload.segments
                    if segment.id in translated
                ],
                "model": payload.model,
                "precision": payload.precision,
                "cached": job.cached,
            }
            if errors:
                result["errors"] = [
                    {"id": segment.id, "message": errors[segment.id]}
                    for segment in payload.segments
                    if segment.id in errors
                ]
            return result
        except asyncio.CancelledError:
            jobs.cancel(job.id)
            raise
        finally:
            disconnected.cancel()
            waiting.cancel()
            jobs.release(job.id)

    return app

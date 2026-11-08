"""FastAPI entry point.  Run:  cd backend && ../.venv/bin/uvicorn app.main:create_app --factory --reload"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import Settings
from .container import Container
from .exceptions import DomainError
from .logging_config import request_id_var, setup_logging

log = logging.getLogger("app.main")
req_log = logging.getLogger("app.request")
VERSION = "0.1.0"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # anything else is replaced: it ends up in logs + headers


_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _security_headers(request: Request, response) -> None:
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.headers.get("x-forwarded-proto") == "https":   # set by the Caddy proxy
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith(_DOC_PATHS):           # Swagger needs scripts/styles from its CDN
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"                    # estimates and notes must not be cached


def create_app(container: Optional[Container] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level, settings.log_format)
    docs = settings.docs_enabled
    app = FastAPI(title="TrueCost API", version=VERSION,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None,
                  description="Ranks Buffalo-area hospitals by a breast-cancer patient's estimated out-of-pocket cost.")
    app.state.container = container or Container.build(settings)
    c = app.state.container
    log.info("startup", extra={
        "version": VERSION, "extractor": c.extractor.name,
        "image_reader": c.transcriber.name if c.transcriber else None,
        "db_path": str(settings.db_path), "db_exists": bool(settings.db_path and os.path.exists(settings.db_path)),
        "cors_origins": list(settings.cors_origins), "cors_origin_regex": settings.cors_origin_regex, "rate_limit_per_min": settings.ai_rate_limit_per_min,
        "access_token_required": bool(settings.api_access_token), "require_session": settings.require_session,
        "session_ttl_min": settings.session_ttl_min, "daily_token_budget": settings.daily_token_budget or None,
        "docs_enabled": settings.docs_enabled,
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")), "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "log_level": settings.log_level, "log_format": settings.log_format})

    if "*" in settings.cors_origins and settings.log_format == "json":      # json logs = running in the container
        log.warning("CORS_ORIGINS is '*': any website can call this API from a browser. "
                    "Set CORS_ORIGINS (and/or CORS_ORIGIN_REGEX) to your frontend URL.")
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_origin_regex=settings.cors_origin_regex,
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID", "Retry-After"])

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        if not _SAFE_REQUEST_ID.match(rid):
            rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        client = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                  or (request.client.host if request.client else "-"))
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last line of defence: log with traceback, return a clean 500
            req_log.exception("unhandled error", extra={"method": request.method, "path": request.url.path})
            response = JSONResponse(status_code=500, content={"detail": "Internal server error.", "request_id": rid})
        ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        _security_headers(request, response)
        level = (logging.DEBUG if request.url.path == "/health" else
                 logging.ERROR if response.status_code >= 500 else
                 logging.WARNING if response.status_code >= 400 else logging.INFO)
        # path only - never the query string or body (they can hold notes and insurance details)
        req_log.log(level, "request", extra={"method": request.method, "path": request.url.path,
                                             "status": response.status_code, "ms": ms, "client": client})
        request_id_var.reset(token)
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        log.warning("rejected: %s", exc, extra={"error": type(exc).__name__, "status": exc.status_code,
                                                "path": request.url.path})
        retry = getattr(exc, "retry_after", None)
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)},
                            headers={"Retry-After": str(retry)} if retry else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # field locations and error types only - never the submitted values
        log.warning("invalid request", extra={"path": request.url.path,
                                              "errors": [{"loc": ".".join(map(str, e["loc"])), "type": e["type"]}
                                                         for e in exc.errors()][:10]})
        return await request_validation_exception_handler(request, exc)

    @app.get("/health", tags=["meta"])
    def health():
        """Liveness: the process is up. Cheap, so the container healthcheck can call it often."""
        return {"status": "ok", "version": VERSION}

    @app.get("/ready", tags=["meta"])
    def ready():
        """Readiness: the price database actually answers. Returns 503 if it does not."""
        try:
            n = len(app.state.container.repo.list_hospitals())
        except Exception:  # noqa: BLE001
            log.exception("readiness check failed")
            return JSONResponse(status_code=503, content={"status": "unavailable", "detail": "price database unreadable"})
        if n == 0:
            return JSONResponse(status_code=503, content={"status": "unavailable", "detail": "price database is empty"})
        return {"status": "ready", "hospitals": n, "version": VERSION}

    app.include_router(router)
    return app

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


def create_app(container: Optional[Container] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level, settings.log_format)
    app = FastAPI(title="TrueCost API", version=VERSION,
                  description="Ranks Buffalo-area hospitals by a breast-cancer patient's estimated out-of-pocket cost.")
    app.state.container = container or Container.build(settings)
    c = app.state.container
    log.info("startup", extra={
        "version": VERSION, "extractor": c.extractor.name,
        "image_reader": c.transcriber.name if c.transcriber else None,
        "db_path": str(settings.db_path), "db_exists": bool(settings.db_path and os.path.exists(settings.db_path)),
        "cors_origins": list(settings.cors_origins), "rate_limit_per_min": settings.ai_rate_limit_per_min,
        "access_token_required": bool(settings.api_access_token),
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")), "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "log_level": settings.log_level, "log_format": settings.log_format})

    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
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
        return {"status": "ok"}

    app.include_router(router)
    return app

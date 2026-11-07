"""FastAPI entry point.  Run:  cd backend && ../.venv/bin/uvicorn app.main:create_app --factory --reload"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import Settings
from .container import Container
from .exceptions import DomainError


def create_app(container: Optional[Container] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="TrueCost API", version="0.1.0",
                  description="Ranks Buffalo-area hospitals by a breast-cancer patient's estimated out-of-pocket cost.")
    app.state.container = container or Container.build(settings)

    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    app.include_router(router)
    return app

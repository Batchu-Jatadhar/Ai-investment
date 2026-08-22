"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import CorrelationIdMiddleware
from app.api.router import api_router
from app.config.settings import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger, set_correlation_id
from app.services.system_events import record_system_event

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    set_correlation_id()
    logger.info("application_startup", extra=settings.safe_dump())
    # Best effort: the API must still start (and /health/db must still report
    # the failure) when the database is unreachable.
    record_system_event("application_startup", settings)
    try:
        yield
    finally:
        logger.info("application_shutdown", extra={"app": settings.app_name})
        record_system_event("application_shutdown", settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format.value)

    app = FastAPI(
        title="AI Investment — Intraday Trading System",
        version=settings.app_version,
        description=(
            "Phase 0 foundation. Live trading is NOT implemented: there is no "
            "order-placement endpoint, no broker write path and no execution engine."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()

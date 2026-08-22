"""Health endpoints.

Both endpoints do real work.  ``/health`` reports process liveness and the
operating mode; ``/health/db`` opens a connection and executes a query, and
returns 503 when that fails.  Neither ever returns a hardcoded "healthy".
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.config.settings import LIVE_TRADING_IMPLEMENTED, Settings, get_settings
from app.infrastructure.db import check_database

router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


class HealthResponse(BaseModel):
    status: str = Field(description="ok | degraded")
    app: str
    version: str
    environment: str
    trading_mode: str
    live_trading_implemented: bool
    uptime_seconds: float


class DatabaseHealthResponse(BaseModel):
    status: str = Field(description="ok | error")
    backend: str
    latency_ms: float | None = None
    migration_revision: str | None = None
    migrated: bool = False
    error: str | None = None


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
def health() -> HealthResponse:
    settings: Settings = get_settings()
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env.value,
        trading_mode=settings.trading_mode.value,
        live_trading_implemented=LIVE_TRADING_IMPLEMENTED,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
    )


@router.get(
    "/health/db",
    response_model=DatabaseHealthResponse,
    summary="Database connectivity",
    responses={503: {"description": "Database unreachable"}},
)
def health_db(response: Response) -> DatabaseHealthResponse:
    result = check_database()
    if not result.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return DatabaseHealthResponse(
            status="error",
            backend=result.backend,
            latency_ms=result.latency_ms,
            error=result.error,
        )
    return DatabaseHealthResponse(
        status="ok",
        backend=result.backend,
        latency_ms=result.latency_ms,
        migration_revision=result.migration_revision,
        migrated=bool(result.details.get("migrated")),
    )

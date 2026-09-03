"""Health endpoints.

Both endpoints do real work.  ``/health`` reports process liveness and the
operating mode; ``/health/db`` opens a connection and executes a query, and
returns 503 when that fails.  Neither ever returns a hardcoded "healthy".
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.config.settings import (
    LIVE_TRADING_IMPLEMENTED,
    MarketDataProviderName,
    Settings,
    get_settings,
)
from app.core.time import utc_now
from app.domain.market.session import MarketSessionCalendar
from app.infrastructure.db import check_database, get_session_factory

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


class ZerodhaHealth(BaseModel):
    configured: bool
    authenticated: bool
    connected: bool
    state: str


class StreamHealth(BaseModel):
    subscribed_instruments: int = 0
    last_tick_at: str | None = None
    last_tick_age_ms: float | None = None
    ticks_accepted: int = 0
    ticks_rejected: int = 0
    gaps_recorded: int = 0


class MarketDataHealthResponse(BaseModel):
    """Actual stream status. Never a fabricated 'healthy'."""

    status: str = Field(description="ok | degraded | not_running | not_configured")
    running_in_this_process: bool
    provider: str
    zerodha: ZerodhaHealth
    stream: StreamHealth
    instruments: dict[str, Any] = Field(default_factory=dict)
    candles: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    session: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


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


def _section(snapshot: dict[str, object], key: str) -> dict[str, Any]:
    """One sub-dictionary of a health snapshot, or an empty one.

    The snapshot is a loosely typed observability bag assembled by the service.
    Narrowing it here keeps the endpoint honest in both directions: the type
    checker can verify the accesses below, and a snapshot missing a section
    reports an empty section rather than raising inside a health check.
    """
    value = snapshot.get(key)
    return dict(value) if isinstance(value, dict) else {}


@router.get(
    "/health/market-data",
    response_model=MarketDataHealthResponse,
    summary="Market-data stream status",
    responses={503: {"description": "Market data is not live"}},
)
def health_market_data(response: Response) -> MarketDataHealthResponse:
    """Report the real state of the market-data pipeline.

    The streamer normally runs as its own process (``aitrade-marketdata``). When
    it is not running here, that is reported plainly - the endpoint reads what
    the database can tell it and says so, rather than inventing a stream status
    it cannot observe.
    """
    from app.services.market_data_service import get_active_service

    settings: Settings = get_settings()
    service = get_active_service()
    provider_name = settings.market_data_provider.value
    configured = (
        settings.zerodha_configured
        if settings.market_data_provider is MarketDataProviderName.ZERODHA
        else True
    )

    if service is not None:
        snapshot = service.health()
        provider = _section(snapshot, "provider")
        stream = _section(snapshot, "stream")
        connected = bool(provider.get("connected"))
        status_value = "ok" if connected else "degraded"
        if not provider.get("configured", configured):
            status_value = "not_configured"
        if status_value != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return MarketDataHealthResponse(
            status=status_value,
            running_in_this_process=bool(snapshot.get("running")),
            provider=str(provider.get("provider", provider_name)),
            zerodha=ZerodhaHealth(
                configured=bool(provider.get("configured", configured)),
                authenticated=bool(provider.get("authenticated")),
                connected=connected,
                state=str(provider.get("state", "UNKNOWN")),
            ),
            stream=StreamHealth(
                **{key: stream[key] for key in StreamHealth.model_fields if key in stream}
            ),
            instruments=_section(snapshot, "instruments"),
            candles=_section(snapshot, "candles"),
            quality=_section(snapshot, "quality"),
            session=_section(snapshot, "session"),
        )

    # No streamer in this process: report configuration and stored facts only.
    from app.infrastructure.repositories.market_data import SqlMarketDataRepository

    repo = SqlMarketDataRepository(get_session_factory())
    try:
        instrument_count = repo.instrument_count()
        retrieved_at = repo.instruments_retrieved_at()
    except Exception:  # noqa: BLE001 - a dead database is /health/db's business
        instrument_count, retrieved_at = 0, None

    now = utc_now()
    state = "NOT_CONFIGURED" if not configured else "DISCONNECTED"
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return MarketDataHealthResponse(
        status="not_configured" if not configured else "not_running",
        running_in_this_process=False,
        provider=provider_name,
        zerodha=ZerodhaHealth(
            configured=configured,
            authenticated=False,
            connected=False,
            state=f"{provider_name.upper()}_{state}",
        ),
        stream=StreamHealth(),
        instruments={
            "count": instrument_count,
            "retrieved_at": retrieved_at.isoformat() if retrieved_at else None,
            "age_seconds": round((now - retrieved_at).total_seconds(), 1) if retrieved_at else None,
            "stale": retrieved_at is None
            or (now - retrieved_at).total_seconds()
            > settings.instrument_master_max_age_hours * 3600,
        },
        session=MarketSessionCalendar.nse_equity().describe(now),
        note=(
            "the market-data streamer is not running in this process; "
            "start it with `aitrade-marketdata`"
        ),
    )

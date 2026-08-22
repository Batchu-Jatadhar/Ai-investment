"""Market-data runner - the composition root for the streaming pipeline.

    aitrade-marketdata
    # or
    python -m app.runtime.market_data

Read-only. This process authenticates for market data, subscribes to the
configured universe, builds candles and persists them. It has no order path and
no way to reach one.

It starts and reports honestly when Zerodha is not configured: the state is
``ZERODHA_NOT_CONFIGURED``, and it does not pretend to have a live feed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from datetime import timedelta

from app.adapters.replay.provider import ReplayMarketDataProvider
from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import ZerodhaError
from app.adapters.zerodha.provider import ReconnectPolicy, ZerodhaMarketDataProvider
from app.config.settings import (
    MarketDataProviderName,
    Settings,
    get_settings,
)
from app.core.logging import configure_logging, get_logger, set_correlation_id
from app.core.time import SystemClock
from app.domain.market.models import CandleInterval, TickMode
from app.domain.market.ports import MarketDataProvider
from app.domain.market.session import MarketSessionCalendar
from app.infrastructure.db import get_session_factory
from app.infrastructure.repositories.market_data import SqlMarketDataRepository
from app.services.instrument_master import InstrumentMaster, InstrumentMasterError
from app.services.market_data_service import MarketDataService, set_active_service

logger = get_logger(__name__)

__all__ = ["build_rest_client", "build_service", "main", "run"]


def build_rest_client(settings: Settings) -> ZerodhaRestClient:
    return ZerodhaRestClient(
        api_key=settings.zerodha_secret("api_key"),
        api_secret=settings.zerodha_secret("api_secret"),
        access_token=settings.zerodha_secret("access_token"),
        api_root=settings.zerodha_api_root,
        timeout_seconds=settings.zerodha_timeout_seconds,
    )


def _intervals(settings: Settings) -> tuple[CandleInterval, ...]:
    return tuple(CandleInterval(name) for name in settings.interval_names)


def build_service(
    settings: Settings,
    *,
    provider: MarketDataProvider | None = None,
    repository: SqlMarketDataRepository | None = None,
) -> tuple[MarketDataService, ZerodhaRestClient | None]:
    """Assemble the pipeline. ``provider`` may be injected for tests."""
    clock = SystemClock()
    repo = repository or SqlMarketDataRepository(get_session_factory())
    client: ZerodhaRestClient | None = None
    source = None

    if provider is None:
        if settings.market_data_provider is MarketDataProviderName.ZERODHA:
            client = build_rest_client(settings)
            source = client
            provider = ZerodhaMarketDataProvider(
                client,
                clock=clock,
                reconnect=ReconnectPolicy(
                    initial_seconds=settings.ws_reconnect_initial_seconds,
                    max_seconds=settings.ws_reconnect_max_seconds,
                    max_attempts=settings.ws_reconnect_max_attempts,
                ),
                ws_root=settings.zerodha_ws_root,
                stale_after=timedelta(seconds=settings.market_data_stream_stale_seconds),
            )
        elif settings.market_data_provider is MarketDataProviderName.REPLAY:
            provider = ReplayMarketDataProvider(clock=clock)
        else:
            provider = ReplayMarketDataProvider(clock=clock, emit_lifecycle=False)

    master = InstrumentMaster(
        repo,
        source,
        clock=clock,
        max_age=timedelta(hours=settings.instrument_master_max_age_hours),
    )
    master.load_from_repository()

    service = MarketDataService(
        provider,
        repo,
        master,
        clock=clock,
        calendar=MarketSessionCalendar.nse_equity(),
        intervals=_intervals(settings),
        mode=TickMode(settings.market_data_mode.value),
        persist_ticks=settings.market_data_persist_ticks,
        tick_stale_after=timedelta(seconds=settings.market_data_tick_stale_seconds),
        flush_interval=timedelta(seconds=settings.market_data_flush_seconds),
    )
    return service, client


async def run(settings: Settings | None = None, *, refresh: bool = True) -> int:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format.value)
    set_correlation_id()

    if settings.market_data_provider is MarketDataProviderName.ZERODHA and not (
        settings.zerodha_configured
    ):
        logger.error(
            "market_data_not_configured",
            extra={
                "state": "ZERODHA_NOT_CONFIGURED",
                "required": ["ZERODHA_API_KEY", "ZERODHA_ACCESS_TOKEN"],
            },
        )
        return 2

    service, client = build_service(settings)
    set_active_service(service)
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        if refresh and client is not None:
            await _refresh_instruments(service, settings)

        await service.subscribe_universe(settings.market_data_universe)
        logger.info(
            "market_data_starting",
            extra={
                "provider": settings.market_data_provider.value,
                "mode": settings.market_data_mode.value,
                "intervals": list(settings.interval_names),
                "universe": len(settings.universe_entries),
            },
        )
        await service.run(stop)
        return 0
    except ZerodhaError as exc:
        logger.error(
            "market_data_provider_error",
            extra={"error_type": type(exc).__name__, "detail": str(exc)},
        )
        return 3
    finally:
        set_active_service(None)
        with contextlib.suppress(Exception):
            await service.provider.disconnect()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


async def _refresh_instruments(service: MarketDataService, settings: Settings) -> None:
    master = service._instruments  # noqa: SLF001 - composition root wiring
    if not master.is_stale():
        logger.info("instrument_master_fresh", extra=master.status())
        return
    try:
        result = await master.refresh()
        logger.info("instrument_master_ready", extra=result.as_dict())
    except (InstrumentMasterError, ZerodhaError) as exc:
        # A stale master is a real risk, so this is loud - but a cached master
        # is still better than no market data at all.
        logger.error(
            "instrument_refresh_failed",
            extra={"error_type": type(exc).__name__, "detail": str(exc)},
        )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="aitrade-marketdata",
        description="Read-only market-data streamer. Places no orders.",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip the instrument-master refresh and use what is stored",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(refresh=not args.no_refresh))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

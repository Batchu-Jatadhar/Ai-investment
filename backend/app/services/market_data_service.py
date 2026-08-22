"""Market-data service: the wiring between a provider and everything downstream.

    provider.stream()
        -> tick validation (data quality)
        -> candle engine (1m, then 5m/15m from completed 1m bars)
        -> repository (ticks, completed candles, connection events, gaps, issues)

The service is provider-agnostic. It runs identically over the live Zerodha
feed and over the replay provider; only the injected provider differs.

A recorded data gap resets the candle engine and the validator's per-instrument
history. That is deliberate: continuity was broken, so a bar spanning the gap
would be a fiction, and the cumulative-volume baseline is no longer meaningful.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.core.time import Clock, SystemClock
from app.domain.market.candles import CandleEngine
from app.domain.market.models import Candle, CandleInterval, MarketTick, TickMode
from app.domain.market.ports import (
    ConnectionEvent,
    ConnectionEventType,
    DataGap,
    MarketDataProvider,
    MarketDataRepository,
    ProviderState,
    TickBatch,
)
from app.domain.market.quality import DataQualityEvent, TickValidator
from app.domain.market.session import MarketSessionCalendar
from app.services.instrument_master import InstrumentMaster

logger = get_logger(__name__)

__all__ = [
    "MarketDataService",
    "get_active_service",
    "set_active_service",
]

_active: MarketDataService | None = None


def set_active_service(service: MarketDataService | None) -> None:
    """Register the service running in *this* process, for health reporting."""
    global _active
    _active = service


def get_active_service() -> MarketDataService | None:
    return _active


class MarketDataService:
    def __init__(
        self,
        provider: MarketDataProvider,
        repository: MarketDataRepository,
        instrument_master: InstrumentMaster,
        *,
        clock: Clock | None = None,
        calendar: MarketSessionCalendar | None = None,
        intervals: tuple[CandleInterval, ...] = (
            CandleInterval.M1,
            CandleInterval.M5,
            CandleInterval.M15,
        ),
        mode: TickMode = TickMode.FULL,
        persist_ticks: bool = True,
        tick_stale_after: timedelta = timedelta(seconds=30),
        flush_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._instruments = instrument_master
        self._clock = clock or SystemClock()
        self._calendar = calendar or MarketSessionCalendar.nse_equity()
        self._mode = mode
        self._persist_ticks = persist_ticks
        self._flush_interval = flush_interval

        self._validator = TickValidator(
            self._instruments.by_token,
            provider=getattr(provider, "name", "unknown"),
            stale_after=tick_stale_after,
        )
        self._candles = CandleEngine(intervals, source=getattr(provider, "name", "unknown"))

        self._running = False
        self._started_at: datetime | None = None
        self._last_tick_at: datetime | None = None
        self._last_flush_at: datetime | None = None
        self._subscribed: tuple[int, ...] = ()
        self.ticks_accepted = 0
        self.ticks_rejected = 0
        self.candles_completed = 0
        self.gaps_recorded = 0

    # ------------------------------------------------------------------ #
    # accessors
    # ------------------------------------------------------------------ #

    @property
    def provider(self) -> MarketDataProvider:
        return self._provider

    @property
    def candle_engine(self) -> CandleEngine:
        return self._candles

    @property
    def validator(self) -> TickValidator:
        return self._validator

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ #
    # subscription
    # ------------------------------------------------------------------ #

    async def subscribe_universe(self, universe: str) -> tuple[int, ...]:
        """Resolve the configured universe and subscribe to it."""
        resolution = self._instruments.resolve(universe)
        tokens = resolution.tokens
        if not tokens:
            logger.warning("universe_resolved_to_nothing", extra=resolution.as_dict())
            return ()
        await self._provider.subscribe(tokens, self._mode)
        self._subscribed = tokens
        logger.info(
            "universe_subscribed",
            extra={
                "tokens": len(tokens),
                "indices": len(resolution.indices),
                "unresolved": len(resolution.unresolved),
                "mode": self._mode.value,
            },
        )
        return tokens

    # ------------------------------------------------------------------ #
    # run loop
    # ------------------------------------------------------------------ #

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Consume the provider stream until it ends or ``stop`` is set."""
        self._running = True
        self._started_at = self._clock.now()
        flusher = asyncio.create_task(self._flush_loop(stop))
        try:
            async for event in self._provider.stream():
                self.handle(event)
                if stop is not None and stop.is_set():
                    break
        finally:
            self._running = False
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
            self._finalise()

    def handle(self, event: object) -> None:
        """Dispatch one stream event. Synchronous and deterministic."""
        if isinstance(event, TickBatch):
            self._handle_ticks(event)
        elif isinstance(event, DataGap):
            self._handle_gap(event)
        elif isinstance(event, ConnectionEvent):
            self._handle_connection(event)

    # ------------------------------------------------------------------ #

    def _handle_ticks(self, batch: TickBatch) -> None:
        now = batch.received_at
        accepted: list[MarketTick] = []
        issues: list[DataQualityEvent] = []
        completed: list[Candle] = []

        for tick in batch.ticks:
            result = self._validator.validate(tick, now=now)
            issues.extend(result.issues)
            if result.tick is None:
                self.ticks_rejected += 1
                continue
            enriched = self._enrich(result.tick)
            accepted.append(enriched)
            completed.extend(self._candles.on_tick(enriched))

        if accepted:
            self.ticks_accepted += len(accepted)
            self._last_tick_at = now
            if self._persist_ticks:
                self._repository.save_ticks(accepted)

        if completed:
            self._store_candles(completed)

        if issues:
            self._repository.record_quality_events(issues)  # type: ignore[attr-defined]

    def _enrich(self, tick: MarketTick) -> MarketTick:
        """Attach symbol and exchange from the instrument master."""
        if tick.tradingsymbol and tick.exchange:
            return tick
        instrument = self._instruments.by_token(tick.instrument_token)
        if instrument is None:
            return tick
        return replace(
            tick,
            tradingsymbol=instrument.tradingsymbol,
            exchange=instrument.exchange,
            is_index=instrument.is_index,
        )

    def _store_candles(self, candles: list[Candle]) -> None:
        written = self._repository.save_candles(candles)
        self.candles_completed += len(candles)
        if written:
            logger.debug(
                "candles_completed",
                extra={"written": written, "batch": len(candles)},
            )

    def _handle_gap(self, gap: DataGap) -> None:
        """A gap breaks continuity, so in-flight bars and baselines are dropped."""
        self._repository.record_data_gap(gap)
        self.gaps_recorded += 1
        self._candles.reset()
        self._validator.reset()
        logger.warning(
            "market_data_gap",
            extra={
                "provider": gap.provider,
                "seconds": round(gap.duration_seconds, 3),
                "reason": gap.reason,
                "instruments": len(gap.instrument_tokens),
            },
        )

    def _handle_connection(self, event: ConnectionEvent) -> None:
        self._repository.record_connection_event(event)
        if event.event_type in (
            ConnectionEventType.AUTH_FAILED,
            ConnectionEventType.PROVIDER_ERROR,
            ConnectionEventType.STREAM_STALE,
        ):
            logger.warning(
                "market_data_event",
                extra={
                    "provider": event.provider,
                    "event": event.event_type.value,
                    "state": event.state.value if event.state else None,
                    **{k: v for k, v in event.detail.items() if k != "findings"},
                },
            )

    # ------------------------------------------------------------------ #

    async def _flush_loop(self, stop: asyncio.Event | None) -> None:
        """Complete bars whose window elapsed without a further tick."""
        seconds = max(0.1, self._flush_interval.total_seconds())
        while self._running and (stop is None or not stop.is_set()):
            await asyncio.sleep(seconds)
            self.flush()

    def flush(self, now: datetime | None = None) -> list[Candle]:
        moment = now or self._clock.now()
        self._last_flush_at = moment
        completed = self._candles.flush(moment)
        if completed:
            self._store_candles(completed)
        return completed

    def _finalise(self) -> None:
        """Close any remaining bars so the last minute of a session is not lost."""
        completed = self._candles.close_all()
        if completed:
            self._store_candles(completed)

    # ------------------------------------------------------------------ #

    def health(self, now: datetime | None = None) -> dict[str, object]:
        moment = now or self._clock.now()
        provider_health = (
            self._provider.health(moment)  # type: ignore[attr-defined]
            if hasattr(self._provider, "health")
            else {"provider": getattr(self._provider, "name", "unknown")}
        )
        age_ms: float | None = None
        if self._last_tick_at is not None:
            age_ms = round((moment - self._last_tick_at).total_seconds() * 1000, 1)
        return {
            "running": self._running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "provider": provider_health,
            "stream": {
                "subscribed_instruments": len(self._provider.subscribed_tokens),
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "last_tick_age_ms": age_ms,
                "ticks_accepted": self.ticks_accepted,
                "ticks_rejected": self.ticks_rejected,
                "gaps_recorded": self.gaps_recorded,
            },
            "quality": self._validator.counters.as_dict(),
            "candles": {
                **self._candles.status(),
                "completed_total": self.candles_completed,
                "last_flush_at": self._last_flush_at.isoformat() if self._last_flush_at else None,
            },
            "instruments": self._instruments.status(moment),
            "session": self._calendar.describe(moment),
        }

    def is_live(self) -> bool:
        """True only when the provider is genuinely connected."""
        return self._running and self._provider.state is ProviderState.CONNECTED

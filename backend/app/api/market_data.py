"""Read-only market-data endpoints.

GET only. Every route reads through :class:`SqlMarketDataRepository`, which is
the same seam the future strategy engine uses - nothing here knows whether the
data arrived from Zerodha, the database or a replay run.

There is no write endpoint in this router, and there is no order route anywhere
in the application.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.errors import NotFoundError
from app.core.time import utc_now
from app.domain.market.models import Candle, CandleInterval, Instrument, MarketTick
from app.infrastructure.db import get_session_factory
from app.infrastructure.repositories.market_data import SqlMarketDataRepository

router = APIRouter(prefix="/market-data", tags=["market-data"])


def _repository() -> SqlMarketDataRepository:
    return SqlMarketDataRepository(get_session_factory())


class InstrumentOut(BaseModel):
    instrument_token: int
    tradingsymbol: str
    name: str
    exchange: str
    segment: str
    instrument_type: str
    tick_size: str
    lot_size: int
    is_index: bool = Field(description="Indices are not directly tradable")
    expiry: str | None = None
    retrieved_at: str | None = None

    @classmethod
    def of(cls, instrument: Instrument) -> InstrumentOut:
        return cls(
            instrument_token=instrument.instrument_token,
            tradingsymbol=instrument.tradingsymbol,
            name=instrument.name,
            exchange=instrument.exchange,
            segment=instrument.segment,
            instrument_type=instrument.instrument_type,
            tick_size=str(instrument.tick_size),
            lot_size=instrument.lot_size,
            is_index=instrument.is_index,
            expiry=instrument.expiry.isoformat() if instrument.expiry else None,
            retrieved_at=instrument.retrieved_at.isoformat() if instrument.retrieved_at else None,
        )


class CandleOut(BaseModel):
    instrument_token: int
    tradingsymbol: str | None
    interval: str
    start_at: str
    end_at: str
    open: str
    high: str
    low: str
    close: str
    volume: int
    tick_count: int
    status: str
    source: str

    @classmethod
    def of(cls, candle: Candle) -> CandleOut:
        return cls(
            instrument_token=candle.instrument_token,
            tradingsymbol=candle.tradingsymbol,
            interval=candle.interval.value,
            start_at=candle.start_at.isoformat(),
            end_at=candle.end_at.isoformat(),
            open=str(candle.open),
            high=str(candle.high),
            low=str(candle.low),
            close=str(candle.close),
            volume=candle.volume,
            tick_count=candle.tick_count,
            status=candle.status.value,
            source=candle.source,
        )


class TickOut(BaseModel):
    instrument_token: int
    tradingsymbol: str | None
    mode: str
    event_time: str
    received_at: str
    last_price: str
    volume: int | None = None
    source: str

    @classmethod
    def of(cls, tick: MarketTick) -> TickOut:
        return cls(
            instrument_token=tick.instrument_token,
            tradingsymbol=tick.tradingsymbol,
            mode=tick.mode.value,
            event_time=tick.event_time.isoformat(),
            received_at=tick.received_at.isoformat(),
            last_price=str(tick.last_price),
            volume=tick.volume,
            source=tick.source,
        )


# Declared before the {exchange}/{tradingsymbol} route: FastAPI matches in
# declaration order, and the generic two-segment route would otherwise swallow
# "by-token/<id>" as exchange="by-token".
@router.get("/instruments/by-token/{instrument_token}", response_model=InstrumentOut)
def get_instrument_by_token(instrument_token: int) -> InstrumentOut:
    instrument = _repository().get_instrument_by_token(instrument_token)
    if instrument is None:
        raise NotFoundError(f"instrument token {instrument_token} is not known")
    return InstrumentOut.of(instrument)


@router.get("/instruments/{exchange}/{tradingsymbol}", response_model=InstrumentOut)
def get_instrument(exchange: str, tradingsymbol: str) -> InstrumentOut:
    """Look up an instrument by its stable identity, not by token."""
    instrument = _repository().get_instrument_by_symbol(exchange, tradingsymbol)
    if instrument is None:
        raise NotFoundError(
            f"no instrument {exchange.upper()}:{tradingsymbol.upper()} in the "
            "instrument master; refresh it or check the symbol"
        )
    return InstrumentOut.of(instrument)


@router.get("/candles/{instrument_token}", response_model=list[CandleOut])
def get_candles(
    instrument_token: int,
    interval: CandleInterval = Query(default=CandleInterval.M1),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
) -> list[CandleOut]:
    """Completed candles. Without a range, the most recent ``limit`` bars."""
    repo = _repository()
    if start is None and end is None:
        candles = repo.recent_candles(instrument_token, interval, limit)
    else:
        window_end = end or utc_now()
        window_start = start or (window_end - timedelta(days=1))
        candles = repo.candles_in_range(instrument_token, interval, window_start, window_end, limit)
    return [CandleOut.of(candle) for candle in candles]


@router.get("/candles/{instrument_token}/latest", response_model=CandleOut)
def get_latest_candle(
    instrument_token: int,
    interval: CandleInterval = Query(default=CandleInterval.M1),
) -> CandleOut:
    candle = _repository().latest_completed_candle(instrument_token, interval)
    if candle is None:
        raise NotFoundError(f"no completed {interval.value} candle for token {instrument_token}")
    return CandleOut.of(candle)


@router.get("/ticks/{instrument_token}/latest", response_model=TickOut)
def get_latest_tick(instrument_token: int) -> TickOut:
    tick = _repository().latest_tick(instrument_token)
    if tick is None:
        raise NotFoundError(f"no ticks stored for token {instrument_token}")
    return TickOut.of(tick)

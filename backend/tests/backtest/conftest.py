"""Deterministic fixtures for the Phase 2 backtest value objects.

Every candle here is built explicitly. Nothing reads a clock, a database or a
file, so these tests are as reproducible as the code they exercise.

Timestamps are chosen to sit on real NSE session boundaries: ``SESSION_OPEN`` is
09:15 IST expressed in UTC, which the Phase 1 candle engine already aligns to
because IST is UTC+05:30 and 30 is a multiple of 1, 5 and 15.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.input import BacktestInput
from app.domain.market.models import (
    Candle,
    CandleInterval,
    CandleStatus,
    Instrument,
)
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.params import OrbParams

RELIANCE_TOKEN = 738561
INFY_TOKEN = 408065

#: 2026-08-21 09:15 IST == 03:45 UTC. A Friday, and a normal trading day.
SESSION_OPEN = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
SESSION_DATE = date(2026, 8, 21)


def make_instrument(
    token: int = RELIANCE_TOKEN,
    tradingsymbol: str = "RELIANCE",
    *,
    tick_size: str = "0.05",
    lot_size: int = 1,
    segment: str = "NSE",
) -> Instrument:
    return Instrument(
        instrument_token=token,
        exchange_token=token >> 8,
        tradingsymbol=tradingsymbol,
        name=tradingsymbol.title(),
        exchange="NSE",
        segment=segment,
        instrument_type="EQ",
        tick_size=Decimal(tick_size),
        lot_size=lot_size,
        source="test",
        retrieved_at=SESSION_OPEN,
    )


def make_candle(
    start_at: datetime,
    interval: CandleInterval = CandleInterval.M5,
    *,
    token: int = RELIANCE_TOKEN,
    open_: str = "1400.00",
    high: str = "1405.00",
    low: str = "1398.00",
    close: str = "1402.00",
    volume: int = 5000,
    status: CandleStatus = CandleStatus.COMPLETED,
) -> Candle:
    return Candle(
        instrument_token=token,
        interval=interval,
        start_at=start_at,
        end_at=start_at + interval.delta,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        status=status,
        tick_count=25,
        source="test",
    )


def make_series(
    count: int,
    interval: CandleInterval,
    *,
    start_at: datetime = SESSION_OPEN,
    token: int = RELIANCE_TOKEN,
) -> tuple[Candle, ...]:
    """``count`` consecutive completed bars, each one paisa above the last."""
    bars: list[Candle] = []
    for index in range(count):
        base = Decimal("1400.00") + Decimal(index) / 100
        bars.append(
            make_candle(
                start_at + interval.delta * index,
                interval,
                token=token,
                open_=str(base),
                high=str(base + Decimal("5.00")),
                low=str(base - Decimal("2.00")),
                close=str(base + Decimal("2.00")),
                volume=5000 + index,
            )
        )
    return tuple(bars)


def make_input(
    *,
    candles_5m: tuple[Candle, ...] | None = None,
    candles_1m: tuple[Candle, ...] | None = None,
    instrument: Instrument | None = None,
    calendar: MarketSessionCalendar | None = None,
    strategy_params: OrbParams | None = None,
    cost_schedule: CostSchedule | None = None,
    slippage_config: SlippageConfig | None = None,
    execution_config: ExecutionConfig | None = None,
) -> BacktestInput:
    """A valid BacktestInput, with any part overridable for a negative test."""
    return BacktestInput(
        instrument=instrument or make_instrument(),
        candles_5m=candles_5m if candles_5m is not None else make_series(12, CandleInterval.M5),
        candles_1m=candles_1m if candles_1m is not None else make_series(60, CandleInterval.M1),
        calendar=calendar or MarketSessionCalendar.nse_equity(),
        strategy_params=strategy_params or OrbParams(),
        cost_schedule=cost_schedule or placeholder_schedule(),
        slippage_config=slippage_config or SlippageConfig(),
        execution_config=execution_config or ExecutionConfig(),
    )


def placeholder_schedule() -> CostSchedule:
    """An explicitly unverified schedule.

    Rates are Phase 2.5 and must be checked against a named source first, so
    everything in Phase 2.0 runs against a schedule that says so.
    """
    return CostSchedule(
        schedule_id="nse-intraday-equity",
        version="0-placeholder",
        effective_from=date(2026, 1, 1),
    )


@pytest.fixture
def instrument() -> Instrument:
    return make_instrument()


@pytest.fixture
def backtest_input() -> BacktestInput:
    return make_input()


@pytest.fixture
def one_minute_later() -> timedelta:
    return timedelta(minutes=1)

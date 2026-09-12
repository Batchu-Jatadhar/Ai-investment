"""Strict 1m -> 5m / 15m aggregation, hand-calculated.

Session 2026-08-21: 09:15 IST is 03:45 UTC.

.. rubric:: The 09:15 five-minute slot

    minute  open     high     low     close    volume
    09:15   100.00   101.00   99.50   100.50   10
    09:16   100.50   102.25  100.25   102.00   20
    09:17   102.00   102.10   98.75    99.00   30
    09:18    99.00   100.00   98.90    99.80   40
    09:19    99.80   101.50   99.60   101.25   50

    open 100.00 (09:15), high 102.25 (09:16), low 98.75 (09:17),
    close 101.25 (09:19), volume 150; 03:45-03:50 UTC.

.. rubric:: The 09:15 fifteen-minute slot

Minute i (0-14) opens at 100 + i, highs 102 + i, lows 99 + i, closes 101 + i,
volume i + 1 - except minute 7 (09:22) spikes to a high of 130 and minute 11
(09:26) dips to a low of 90.

    open 100 (minute 0), high 130 (09:22), low 90 (09:26),
    close 115 (minute 14), volume 1 + 2 + ... + 15 = 120; 03:45-04:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.aggregation import (
    AggregationResult,
    IncompleteSlot,
    InvalidMinuteSeriesError,
    aggregate_minutes,
)
from app.domain.market.models import Candle, CandleInterval, CandleStatus

M1, M5, M15 = CandleInterval.M1, CandleInterval.M5, CandleInterval.M15
OPEN = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)  # 09:15 IST


def minute(
    offset: int,
    o: str | int,
    h: str | int,
    lo: str | int,
    c: str | int,
    volume: int,
    **overrides: object,
) -> Candle:
    start = OPEN + timedelta(minutes=offset)
    values: dict[str, object] = {
        "instrument_token": 738561,
        "interval": M1,
        "start_at": start,
        "end_at": start + M1.delta,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(lo),
        "close": Decimal(c),
        "volume": volume,
        "status": CandleStatus.COMPLETED,
        "tick_count": 1,
        "source": "zerodha_historical",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
    }
    values.update(overrides)
    return Candle(**values)  # type: ignore[arg-type]


FIRST_SLOT = (
    minute(0, "100.00", "101.00", "99.50", "100.50", 10),
    minute(1, "100.50", "102.25", "100.25", "102.00", 20),
    minute(2, "102.00", "102.10", "98.75", "99.00", 30),
    minute(3, "99.00", "100.00", "98.90", "99.80", 40),
    minute(4, "99.80", "101.50", "99.60", "101.25", 50),
)


def quarter_hour() -> tuple[Candle, ...]:
    bars = []
    for i in range(15):
        high = 130 if i == 7 else 102 + i
        low = 90 if i == 11 else 99 + i
        bars.append(minute(i, 100 + i, high, low, 101 + i, i + 1))
    return tuple(bars)


def test_five_minutes_make_one_exact_five_minute_bar() -> None:
    result = aggregate_minutes(FIRST_SLOT, M5)

    assert result.incomplete == ()
    (bar,) = result.candles
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        Decimal("100.00"),
        Decimal("102.25"),
        Decimal("98.75"),
        Decimal("101.25"),
        150,
    )
    assert (bar.start_at, bar.end_at) == (OPEN, datetime(2026, 8, 21, 3, 50, tzinfo=UTC))
    assert bar.start_at.tzinfo is UTC
    assert (bar.interval, bar.status, bar.tick_count) == (M5, CandleStatus.COMPLETED, 5)
    assert (bar.instrument_token, bar.tradingsymbol, bar.exchange, bar.source) == (
        738561,
        "RELIANCE",
        "NSE",
        "zerodha_historical",
    )


def test_fifteen_minutes_make_one_exact_fifteen_minute_bar() -> None:
    (bar,) = aggregate_minutes(quarter_hour(), M15).candles
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        Decimal(100),
        Decimal(130),
        Decimal(90),
        Decimal(115),
        120,
    )
    assert (bar.start_at, bar.end_at) == (OPEN, datetime(2026, 8, 21, 4, 0, tzinfo=UTC))


def test_consecutive_slots_and_a_missing_minute() -> None:
    """09:15-09:29 minus 09:22: the 09:15 and 09:25 slots aggregate; the 09:20
    slot has four minutes, so it produces nothing and names 09:22."""
    minutes = tuple(m for m in quarter_hour() if m.start_at != OPEN + timedelta(minutes=7))

    result = aggregate_minutes(minutes, M5)

    assert [bar.start_at for bar in result.candles] == [OPEN, OPEN + timedelta(minutes=10)]
    assert result.incomplete == (
        IncompleteSlot(OPEN + timedelta(minutes=5), (OPEN + timedelta(minutes=7),)),
    )
    # The 09:25 bar is built from its own five minutes only: open 110, close 115.
    assert (result.candles[1].open, result.candles[1].close) == (Decimal(110), Decimal(115))
    # And the quarter hour as a whole is incomplete for the same reason.
    assert aggregate_minutes(minutes, M15) == AggregationResult(
        (), (IncompleteSlot(OPEN, (OPEN + timedelta(minutes=7),)),)
    )


def test_slots_align_to_the_0915_ist_open() -> None:
    """A 09:14 minute belongs to the slot before the open, not to 09:15's."""
    before_open = minute(-1, 100, 101, 99, 100, 5)

    result = aggregate_minutes((before_open, *FIRST_SLOT), M5)

    assert [bar.start_at for bar in result.candles] == [OPEN]
    slot = OPEN - timedelta(minutes=5)  # 09:10 IST
    assert result.incomplete == (
        IncompleteSlot(slot, tuple(slot + timedelta(minutes=i) for i in range(4))),
    )


def test_the_same_minutes_always_aggregate_identically() -> None:
    minutes = quarter_hour()[:13]
    assert aggregate_minutes(minutes, M5) == aggregate_minutes(minutes, M5)


@pytest.mark.parametrize(
    ("minutes", "interval"),
    [
        pytest.param(
            (*FIRST_SLOT[:4], minute(4, 99, 101, 99, 100, 5, status=CandleStatus.IN_PROGRESS)),
            M5,
            id="in-progress-minute",
        ),
        pytest.param((FIRST_SLOT[1], FIRST_SLOT[0]), M5, id="out-of-order"),
        pytest.param((FIRST_SLOT[0], FIRST_SLOT[0]), M5, id="duplicate"),
        pytest.param(
            (FIRST_SLOT[0], minute(1, 100, 101, 99, 100, 5, instrument_token=408065)),
            M5,
            id="two-instruments",
        ),
        pytest.param(
            (FIRST_SLOT[0], minute(1, 100, 101, 99, 100, 5, source="zerodha")),
            M5,
            id="two-sources",
        ),
        pytest.param(
            (minute(0, 100, 101, 99, 100, 5, interval=M5, end_at=OPEN + M5.delta),),
            M15,
            id="not-a-minute-bar",
        ),
        pytest.param(FIRST_SLOT, M1, id="target-is-1m"),
    ],
)
def test_invalid_source_input_is_rejected(
    minutes: tuple[Candle, ...], interval: CandleInterval
) -> None:
    with pytest.raises(InvalidMinuteSeriesError):
        aggregate_minutes(minutes, interval)

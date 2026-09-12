"""The ORB context's prior ATR is derived from the BacktestInput alone.

.. rubric:: Hand calculation

Wednesday 2026-08-19 holds 16 bars, every close 1000.00, so each true range is
simply that bar's high minus low:

    bar 0          1001.00 / 999.00    no previous close, not counted
    bars 1-7       1004.00 / 996.00    TR  8.00 each
    bars 8-14      1006.00 / 994.00    TR 12.00 each
    bar 15         1012.00 / 988.00    TR 24.00

    seed    (7 x 8.00 + 7 x 12.00) / 14            = 140 / 14 = 10.00
    Wilder  (10.00 x 13 + 24.00) / 14              = 154 / 14 = 11.00

So Thursday 2026-08-20 is handed an ATR of exactly 11. With only bars 0-14
(15 bars, the minimum for ATR(14)) it is the seed, 10.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.domain.backtest.input import BacktestInput
from app.domain.market.models import Candle, CandleInterval
from tests.backtest.conftest import make_candle, make_input

DAY_1 = datetime(2026, 8, 19, 3, 45, tzinfo=UTC)  # 09:15 IST
DAY_2 = DAY_1 + timedelta(days=1)
DAY_3 = DAY_1 + timedelta(days=2)


def bar(open_at: datetime, index: int, high: str, low: str, close: str = "1000.00") -> Candle:
    return make_candle(
        open_at + CandleInterval.M5.delta * index, open_="1000.00", high=high, low=low, close=close
    )


HISTORY = (
    bar(DAY_1, 0, "1001.00", "999.00"),
    *(bar(DAY_1, i, "1004.00", "996.00") for i in range(1, 8)),
    *(bar(DAY_1, i, "1006.00", "994.00") for i in range(8, 15)),
    bar(DAY_1, 15, "1012.00", "988.00"),
)
#: Day 2's opening range (09:15-09:25) and one bar after it.
DAY_2_BARS = tuple(bar(DAY_2, i, "1003.00", "997.00") for i in range(4))
VIOLENT = ("1500.00", "500.00", "1400.00")


def one_minute_per_session(candles_5m: tuple[Candle, ...]) -> tuple[Candle, ...]:
    firsts: dict[date, Candle] = {}
    for candle in candles_5m:
        firsts.setdefault(candle.start_at.date(), candle)
    return tuple(make_candle(c.start_at, CandleInterval.M1) for c in firsts.values())


def backtest_input(candles_5m: tuple[Candle, ...]) -> BacktestInput:
    return make_input(candles_5m=candles_5m, candles_1m=one_minute_per_session(candles_5m))


BASELINE = backtest_input(HISTORY + DAY_2_BARS)


def test_exact_wilder_atr_from_prior_completed_bars_only() -> None:
    assert BASELINE.prior_atr(DAY_2.date()) == Decimal("11")
    # A session's own bars never count: day 1 has 16 bars but nothing before it.
    assert BASELINE.prior_atr(DAY_1.date()) is None


def test_current_session_opening_range_cannot_affect_the_atr() -> None:
    wild_open = tuple(bar(DAY_2, i, *VIOLENT) for i in range(3)) + DAY_2_BARS[3:]
    assert backtest_input(HISTORY + wild_open).prior_atr(DAY_2.date()) == Decimal("11")


def test_future_bars_cannot_affect_the_atr() -> None:
    later_today = DAY_2_BARS[:3] + (bar(DAY_2, 3, *VIOLENT),)
    tomorrow = tuple(bar(DAY_3, i, *VIOLENT) for i in range(20))
    altered = backtest_input(HISTORY + later_today + tomorrow)
    assert altered.prior_atr(DAY_2.date()) == Decimal("11")


def test_insufficient_prior_history_is_explicitly_none() -> None:
    assert backtest_input(HISTORY[:14] + DAY_2_BARS).prior_atr(DAY_2.date()) is None
    assert backtest_input(HISTORY[:15] + DAY_2_BARS).prior_atr(DAY_2.date()) == Decimal("10")


def test_same_input_gives_same_atr() -> None:
    again = backtest_input(HISTORY + DAY_2_BARS)
    assert again.prior_atr(DAY_2.date()) == BASELINE.prior_atr(DAY_2.date())
    assert again.fingerprint() == BASELINE.fingerprint()


def test_changing_atr_source_data_changes_the_fingerprint() -> None:
    altered = backtest_input((*HISTORY[:15], bar(DAY_1, 15, "1026.00", "974.00")) + DAY_2_BARS)
    # TR 52 instead of 24: (10 x 13 + 52) / 14 = 13.
    assert altered.prior_atr(DAY_2.date()) == Decimal("13")
    assert altered.fingerprint() != BASELINE.fingerprint()
    assert BASELINE.canonical_payload()["prior_atr"] == {
        "method": "wilder",
        "period": "14",
        "source": "completed signal bars strictly before the session",
    }

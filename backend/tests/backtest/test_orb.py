"""ORB breakout detection against hand-built bars.

The opening range is fixed at 1390-1412 throughout, so every expectation below
can be read off the numbers in the test itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.indicators import OpeningRange, opening_range
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.contract import SignalDirection
from app.domain.strategy.orb import breakout_direction
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_DATE, SESSION_OPEN, make_candle

#: 09:15-09:30 IST == 03:45-04:00 UTC, low 1390, high 1412.
RANGE = OpeningRange(
    session_date=SESSION_DATE,
    start_at=SESSION_OPEN,
    end_at=datetime(2026, 8, 21, 4, 0, tzinfo=UTC),
    high=Decimal("1412"),
    low=Decimal("1390"),
    bar_count=3,
)

#: 09:30 IST - the first bar that could possibly break the range.
FIRST_DECIDABLE = datetime(2026, 8, 21, 4, 0, tzinfo=UTC)


def bar(
    *,
    close: str,
    high: str,
    low: str,
    start_at: datetime = FIRST_DECIDABLE,
    status: CandleStatus = CandleStatus.COMPLETED,
) -> Candle:
    return make_candle(
        start_at,
        CandleInterval.M5,
        token=RELIANCE_TOKEN,
        open_="1400",
        high=high,
        low=low,
        close=close,
        status=status,
    )


class TestBreakoutDirection:
    def test_a_close_above_the_range_is_a_long_breakout(self) -> None:
        found = bar(close="1415", high="1416", low="1405")
        assert breakout_direction(found, RANGE) is SignalDirection.LONG

    def test_a_close_below_the_range_is_a_short_breakout(self) -> None:
        found = bar(close="1385", high="1400", low="1384")
        assert breakout_direction(found, RANGE) is SignalDirection.SHORT

    def test_a_close_inside_the_range_is_not_a_breakout(self) -> None:
        found = bar(close="1400", high="1405", low="1395")
        assert breakout_direction(found, RANGE) is None

    def test_a_wick_above_the_range_is_not_enough(self) -> None:
        """Rejected at the level. Treating this as a breakout is how a strategy
        ends up buying the high of the day."""
        found = bar(close="1405", high="1420", low="1398")
        assert found.high > RANGE.high
        assert breakout_direction(found, RANGE) is None

    def test_a_wick_below_the_range_is_not_enough(self) -> None:
        found = bar(close="1395", high="1405", low="1380")
        assert found.low < RANGE.low
        assert breakout_direction(found, RANGE) is None

    @pytest.mark.parametrize("touch", ["1412", "1390"])
    def test_an_exact_touch_of_a_boundary_is_not_a_breakout(self, touch: str) -> None:
        """The comparisons are strict. The boundary is where everyone else's
        stops sit, so a bar that merely reached one is trading noise."""
        found = bar(close=touch, high="1412", low="1390")
        assert breakout_direction(found, RANGE) is None

    def test_a_bar_that_is_still_forming_the_range_cannot_break_it(self) -> None:
        """09:25 is the third opening-range bar. Its close is above the range
        high only because it is one of the bars that set that high - without
        this guard the range would break out of itself.
        """
        third = bar(
            close="1412",
            high="1412",
            low="1400",
            start_at=SESSION_OPEN + CandleInterval.M5.delta * 2,
        )
        assert third.start_at < RANGE.end_at
        assert breakout_direction(third, RANGE) is None

    def test_an_in_progress_bar_is_rejected_rather_than_ignored(self) -> None:
        """Returning None here would be indistinguishable from "no breakout"
        and would hide the wiring error that produced it."""
        live = bar(close="1415", high="1416", low="1405", status=CandleStatus.IN_PROGRESS)
        with pytest.raises(ValueError, match="in_progress"):
            breakout_direction(live, RANGE)


def test_detection_composes_with_the_opening_range_indicator() -> None:
    """The two halves fit: a range measured from real bars, broken by the next.

    Built separately in every other test so a failure localises, but wired up
    once here - a mismatch in the window's end would otherwise only surface
    much later.
    """
    opening_bars = (
        make_candle(SESSION_OPEN, CandleInterval.M5, high="1405", low="1398", close="1400"),
        make_candle(
            SESSION_OPEN + CandleInterval.M5.delta,
            CandleInterval.M5,
            high="1402",
            low="1390",
            close="1395",
        ),
        make_candle(
            SESSION_OPEN + CandleInterval.M5.delta * 2,
            CandleInterval.M5,
            high="1412",
            low="1400",
            close="1410",
        ),
    )
    measured = opening_range(opening_bars, SESSION_DATE, MarketSessionCalendar.nse_equity())
    assert (measured.high, measured.low) == (Decimal("1412"), Decimal("1390"))

    breakout = make_candle(
        measured.end_at, CandleInterval.M5, high="1418", low="1409", close="1416"
    )
    assert breakout_direction(breakout, measured) is SignalDirection.LONG

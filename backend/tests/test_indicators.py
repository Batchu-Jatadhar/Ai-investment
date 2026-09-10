"""Indicator correctness.

Every expectation here is hand-computed and written out in the test, so a
failure says which arithmetic is wrong rather than that two opaque numbers
differ. Bars are built explicitly - nothing reads a clock, a file or a database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from app.domain.indicators import (
    IndicatorError,
    average_true_range,
    opening_range,
    true_range,
)
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.session import MarketSessionCalendar, SessionWindow

RELIANCE_TOKEN = 738561

#: 2026-08-21 09:15 IST == 03:45 UTC. A Friday, and a normal trading day.
SESSION_OPEN = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
SESSION_DATE = date(2026, 8, 21)


def bar(
    index: int,
    high: str,
    low: str,
    close: str,
    *,
    interval: CandleInterval = CandleInterval.M5,
    token: int = RELIANCE_TOKEN,
    status: CandleStatus = CandleStatus.COMPLETED,
    start_at: datetime | None = None,
) -> Candle:
    """One completed bar, ``index`` slots after the session open.

    ``open`` is irrelevant to true range and is set to the close so the bars
    stay easy to read.
    """
    start = start_at if start_at is not None else SESSION_OPEN + interval.delta * index
    return Candle(
        instrument_token=token,
        interval=interval,
        start_at=start,
        end_at=start + interval.delta,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
        status=status,
        source="test",
    )


#: Four bars whose true ranges are exactly 10, 8 and 12, so ATR(3) is exactly
#: 30 / 3 = 10 and the assertion needs no rounding to be readable.
#:
#:   bar 0  H 105  L  95  C 100   - no previous close, supplies one
#:   bar 1  H 110  L 100  C 108   - TR = max(10, |110-100|, |100-100|) = 10
#:   bar 2  H 112  L 104  C 106   - TR = max( 8, |112-108|, |104-108|) =  8
#:   bar 3  H 118  L 106  C 112   - TR = max(12, |118-106|, |106-106|) = 12
EXACT_SERIES = (
    bar(0, "105", "95", "100"),
    bar(1, "110", "100", "108"),
    bar(2, "112", "104", "106"),
    bar(3, "118", "106", "112"),
)


class TestTrueRange:
    def test_a_quiet_bar_uses_its_own_high_low(self) -> None:
        candle = bar(1, "110", "100", "105")
        assert true_range(candle, Decimal("104")) == Decimal("10")

    def test_a_gap_up_measures_from_the_previous_close(self) -> None:
        """The whole point of true range: a bar that opens away from the last
        close travelled the gap as well as its own range."""
        candle = bar(1, "120", "115", "118")
        assert candle.range == Decimal("5")
        assert true_range(candle, Decimal("100")) == Decimal("20")

    def test_a_gap_down_measures_from_the_previous_close(self) -> None:
        candle = bar(1, "85", "80", "82")
        assert true_range(candle, Decimal("100")) == Decimal("20")


class TestAverageTrueRange:
    def test_the_seed_is_the_mean_of_the_first_period_true_ranges(self) -> None:
        assert average_true_range(EXACT_SERIES, period=3) == Decimal("10")

    def test_a_later_bar_is_folded_in_with_wilders_recurrence(self) -> None:
        """ATR = (previous * (period - 1) + TR) / period.

        Bar 4 is H 115 L 108 C 110 against a previous close of 112, so its true
        range is max(7, |115-112|, |108-112|) = 7 and ATR becomes
        (10 * 2 + 7) / 3 = 9.
        """
        extended = (*EXACT_SERIES, bar(4, "115", "108", "110"))
        assert average_true_range(extended, period=3) == Decimal("9")

    def test_a_gap_widens_the_average(self) -> None:
        """Same first two bars, but bar 3 gaps up instead of trading through.

        Its high-low range is only 4, yet it moved 14 from the previous close of
        106, so the true range is 14 and ATR(3) is (10 + 8 + 14) / 3.
        """
        gapped = (*EXACT_SERIES[:3], bar(3, "120", "116", "118"))
        assert true_range(gapped[3], gapped[2].close) == Decimal("14")
        assert average_true_range(gapped, period=3) == Decimal("32") / Decimal("3")

    def test_the_result_is_an_exact_decimal_never_a_float(self) -> None:
        gapped = (*EXACT_SERIES[:3], bar(3, "120", "116", "118"))
        atr = average_true_range(gapped, period=3)
        assert isinstance(atr, Decimal)
        assert not isinstance(atr, float)
        assert atr.quantize(Decimal("0.000001")) == Decimal("10.666667")

    def test_only_the_bars_supplied_are_read(self) -> None:
        """A prefix must yield the value that was knowable at its end.

        The extra bar is deliberately violent - a 100-point range - so if it
        leaked into the prefix's average the assertion could not pass by luck.
        """
        future = bar(4, "300", "200", "250")
        prefix = average_true_range(EXACT_SERIES, period=3)
        assert prefix == Decimal("10")
        assert average_true_range((*EXACT_SERIES, future), period=3) != prefix

    def test_a_short_series_is_rejected_rather_than_approximated(self) -> None:
        """One bar short: three bars give only two true ranges, not three."""
        with pytest.raises(IndicatorError, match="at least 4 bars"):
            average_true_range(EXACT_SERIES[:3], period=3)

    def test_an_empty_series_is_rejected(self) -> None:
        with pytest.raises(IndicatorError, match="empty"):
            average_true_range((), period=3)

    def test_an_in_progress_bar_is_rejected(self) -> None:
        live = (*EXACT_SERIES[:3], bar(3, "118", "106", "112", status=CandleStatus.IN_PROGRESS))
        with pytest.raises(IndicatorError, match="in_progress"):
            average_true_range(live, period=3)

    def test_a_second_instrument_is_rejected(self) -> None:
        mixed = (*EXACT_SERIES[:3], bar(3, "118", "106", "112", token=408065))
        with pytest.raises(IndicatorError, match="one instrument"):
            average_true_range(mixed, period=3)

    def test_mixed_intervals_are_rejected(self) -> None:
        mixed = (*EXACT_SERIES[:3], bar(3, "118", "106", "112", interval=CandleInterval.M15))
        with pytest.raises(IndicatorError, match="mixing intervals"):
            average_true_range(mixed, period=3)

    def test_an_out_of_order_series_is_rejected(self) -> None:
        reordered = (EXACT_SERIES[0], EXACT_SERIES[2], EXACT_SERIES[1], EXACT_SERIES[3])
        with pytest.raises(IndicatorError, match="ascending"):
            average_true_range(reordered, period=3)

    def test_a_meaningless_period_is_rejected(self) -> None:
        with pytest.raises(IndicatorError, match="at least 1"):
            average_true_range(EXACT_SERIES, period=0)


#: The approved 09:15-09:30 IST window: exactly three completed 5-minute bars.
#: The extreme high and the extreme low sit on different bars, and neither on
#: the first, so an implementation that read only one bar could not pass.
#:
#:   09:15  H 1405  L 1398
#:   09:20  H 1402  L 1390  <- session low
#:   09:25  H 1412  L 1400  <- session high
OPENING_BARS = (
    bar(0, "1405", "1398", "1400"),
    bar(1, "1402", "1390", "1395"),
    bar(2, "1412", "1400", "1410"),
)

NSE = MarketSessionCalendar.nse_equity()


class TestOpeningRange:
    def test_the_range_spans_all_three_opening_bars(self) -> None:
        found = opening_range(OPENING_BARS, SESSION_DATE, NSE)
        assert found.high == Decimal("1412")
        assert found.low == Decimal("1390")
        assert found.width == Decimal("22")
        assert found.bar_count == 3

    def test_the_window_is_09_15_to_09_30_ist(self) -> None:
        found = opening_range(OPENING_BARS, SESSION_DATE, NSE)
        assert found.session_date == SESSION_DATE
        assert found.start_at == SESSION_OPEN
        assert found.end_at == datetime(2026, 8, 21, 4, 0, tzinfo=UTC)

    def test_afternoon_bars_cannot_reach_the_morning_range(self) -> None:
        """Only the window is read, however much data is handed over.

        The later bars are deliberately extreme, so if any of them leaked into
        the range the assertion could not pass by luck.
        """
        rest_of_day = (bar(20, "1600", "1300", "1500"), bar(21, "1700", "1200", "1400"))
        assert opening_range((*OPENING_BARS, *rest_of_day), SESSION_DATE, NSE) == opening_range(
            OPENING_BARS, SESSION_DATE, NSE
        )

    def test_a_missing_opening_bar_is_rejected(self) -> None:
        """Two of three bars is not a narrower range, it is a wrong number."""
        with pytest.raises(IndicatorError, match="missing 09:20:00 IST"):
            opening_range((OPENING_BARS[0], OPENING_BARS[2]), SESSION_DATE, NSE)

    def test_an_in_progress_opening_bar_is_rejected(self) -> None:
        live = (
            *OPENING_BARS[:2],
            bar(2, "1412", "1400", "1410", status=CandleStatus.IN_PROGRESS),
        )
        with pytest.raises(IndicatorError, match="in_progress"):
            opening_range(live, SESSION_DATE, NSE)

    def test_a_non_trading_day_is_rejected(self) -> None:
        """2026-08-22 is a Saturday, so it has no session to open."""
        with pytest.raises(IndicatorError, match="not a trading day"):
            opening_range(OPENING_BARS, date(2026, 8, 22), NSE)

    def test_a_holiday_is_rejected(self) -> None:
        closed = MarketSessionCalendar.nse_equity(holidays=[SESSION_DATE])
        with pytest.raises(IndicatorError, match="not a trading day"):
            opening_range(OPENING_BARS, SESSION_DATE, closed)

    def test_a_session_that_does_not_start_on_a_bar_boundary_is_rejected(self) -> None:
        """Bar boundaries are epoch-aligned, so a 09:16 session has no first bar.

        Reported as the structural problem it is, rather than as three missing
        bars, which is what a naive lookup would have said.
        """
        odd = MarketSessionCalendar(
            window=SessionWindow(
                name="ODD",
                pre_open_start=time(9, 0),
                open_time=time(9, 16),
                close_time=time(15, 30),
                post_close_end=time(16, 0),
            )
        )
        with pytest.raises(IndicatorError, match="not aligned"):
            opening_range(OPENING_BARS, SESSION_DATE, odd)

    def test_a_window_that_ends_mid_bar_is_rejected(self) -> None:
        with pytest.raises(IndicatorError, match="not a whole number"):
            opening_range(OPENING_BARS, SESSION_DATE, NSE, opening_range_minutes=7)

    def test_the_window_length_is_configurable_but_must_be_positive(self) -> None:
        first_bar_only = opening_range(OPENING_BARS, SESSION_DATE, NSE, opening_range_minutes=5)
        assert first_bar_only.bar_count == 1
        assert first_bar_only.high == Decimal("1405")
        with pytest.raises(IndicatorError, match="must be positive"):
            opening_range(OPENING_BARS, SESSION_DATE, NSE, opening_range_minutes=0)

    def test_the_range_is_immutable(self) -> None:
        found = opening_range(OPENING_BARS, SESSION_DATE, NSE)
        with pytest.raises(AttributeError):
            found.high = Decimal("9999")  # type: ignore[misc]

"""Indicator correctness.

Every expectation here is hand-computed and written out in the test, so a
failure says which arithmetic is wrong rather than that two opaque numbers
differ. Bars are built explicitly - nothing reads a clock, a file or a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.indicators import (
    IndicatorError,
    average_true_range,
    true_range,
)
from app.domain.market.models import Candle, CandleInterval, CandleStatus

RELIANCE_TOKEN = 738561

#: 2026-08-21 09:15 IST == 03:45 UTC. A Friday, and a normal trading day.
SESSION_OPEN = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


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

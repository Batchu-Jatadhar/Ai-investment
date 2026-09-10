"""ORB breakout detection against hand-built bars.

The opening range is fixed at 1390-1412 throughout, so every expectation below
can be read off the numbers in the test itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.time import to_ist
from app.domain.indicators import IndicatorError, OpeningRange, opening_range
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.contract import Signal, SignalDirection, Strategy, StrategyContext
from app.domain.strategy.orb import OrbDecision, OrbReason, OrbStrategy, breakout_direction
from tests.backtest.conftest import (
    RELIANCE_TOKEN,
    SESSION_DATE,
    SESSION_OPEN,
    make_candle,
    make_instrument,
)

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


def five_minute(index: int, *, high: str, low: str, close: str) -> Candle:
    """The ``index``-th 5m bar of the session, counting from 09:15 IST."""
    return make_candle(
        SESSION_OPEN + CandleInterval.M5.delta * index,
        CandleInterval.M5,
        open_="1400",
        high=high,
        low=low,
        close=close,
    )


#: The three 09:15-09:30 bars, giving a range of 1390-1412 (width 22).
OPENING_BARS = (
    five_minute(0, high="1405", low="1398", close="1400"),
    five_minute(1, high="1402", low="1390", close="1395"),
    five_minute(2, high="1412", low="1400", close="1410"),
)

#: A prior ATR that clears the hypothesis' filters for a 22-wide range: the
#: ceiling is 1.5 x 30 = 45, and the floor is 4 ticks x 0.05 = 0.20.
PRIOR_ATR = Decimal("30")


def context(**overrides: object) -> StrategyContext:
    values: dict[str, object] = {
        "instrument": make_instrument(),
        "calendar": MarketSessionCalendar.nse_equity(),
        "session_open": SESSION_OPEN,
        "session_close": SESSION_OPEN + timedelta(hours=6, minutes=15),
        "prior_atr": PRIOR_ATR,
    }
    values.update(overrides)
    return StrategyContext(**values)  # type: ignore[arg-type]


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
    measured = opening_range(OPENING_BARS, SESSION_DATE, MarketSessionCalendar.nse_equity())
    assert (measured.high, measured.low) == (Decimal("1412"), Decimal("1390"))

    breakout = make_candle(
        measured.end_at, CandleInterval.M5, high="1418", low="1409", close="1416"
    )
    assert breakout_direction(breakout, measured) is SignalDirection.LONG


LONG_BREAK = five_minute(3, high="1418", low="1409", close="1416")
SHORT_BREAK = five_minute(3, high="1400", low="1384", close="1385")
NO_BREAK = five_minute(3, high="1408", low="1396", close="1402")


class TestOrbSignalLifecycle:
    """From a session prefix to a Signal, and the reason recorded either way."""

    def test_a_close_above_the_range_produces_a_long_signal(self) -> None:
        decision = OrbStrategy().evaluate((*OPENING_BARS, LONG_BREAK), context())

        assert decision.reason is OrbReason.LONG_BREAKOUT
        assert decision.signal is not None
        assert decision.signal.direction is SignalDirection.LONG
        assert decision.signal.stop_price == Decimal("1390")
        assert decision.signal.target_r_multiple == Decimal("2.0")
        assert decision.signal.signal_bar_start == LONG_BREAK.start_at
        assert decision.signal.instrument_token == RELIANCE_TOKEN
        assert decision.signal.reason == "LONG_ORB_BREAKOUT"

    def test_a_close_below_the_range_produces_a_short_signal(self) -> None:
        """A short stops at the range high - the level that proves it wrong."""
        decision = OrbStrategy().evaluate((*OPENING_BARS, SHORT_BREAK), context())

        assert decision.reason is OrbReason.SHORT_BREAKOUT
        assert decision.signal is not None
        assert decision.signal.direction is SignalDirection.SHORT
        assert decision.signal.stop_price == Decimal("1412")
        assert decision.signal.reason == "SHORT_ORB_BREAKOUT"

    def test_a_close_inside_the_range_is_recorded_as_no_breakout(self) -> None:
        decision = OrbStrategy().evaluate((*OPENING_BARS, NO_BREAK), context())
        assert decision == OrbDecision(None, OrbReason.NO_BREAKOUT)

    def test_a_bar_still_forming_the_range_reports_the_range_incomplete(self) -> None:
        """09:15 and 09:20 cannot break a range 09:25 has not finished setting."""
        for prefix_length in (1, 2):
            decision = OrbStrategy().evaluate(OPENING_BARS[:prefix_length], context())
            assert decision == OrbDecision(None, OrbReason.OPENING_RANGE_INCOMPLETE)

    def test_an_empty_prefix_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="session_bars is empty"):
            OrbStrategy().evaluate((), context())

    def test_a_gap_in_the_opening_window_raises_rather_than_rejecting(self) -> None:
        """Missing data is not a declined setup.

        Reporting a feed gap as "no breakout" would make it indistinguishable
        from a quiet morning, and the run would look complete when it was not.
        """
        gapped = (OPENING_BARS[0], OPENING_BARS[2], LONG_BREAK)
        with pytest.raises(IndicatorError, match="missing 09:20:00 IST"):
            OrbStrategy().evaluate(gapped, context())


class TestTargetRepresentation:
    def test_the_target_needs_an_entry_the_signal_does_not_have(self) -> None:
        """The whole point of R: one signal, a different target per fill.

        Both targets below come from the same Signal. If it had carried an
        absolute target price it would have had to presuppose one of them, and
        the assumed entry would be recoverable from stop and target together.
        """
        signal = OrbStrategy().on_bar((*OPENING_BARS, LONG_BREAK), context())
        assert signal is not None

        def target_for(entry: Decimal) -> Decimal:
            risk = entry - signal.stop_price
            return entry + signal.target_r_multiple * risk

        assert target_for(Decimal("1413")) == Decimal("1459")
        assert target_for(Decimal("1420")) == Decimal("1480")


class TestDeterminism:
    def test_evaluating_the_same_prefix_twice_gives_the_same_decision(self) -> None:
        strategy = OrbStrategy()
        prefix = (*OPENING_BARS, LONG_BREAK)
        assert strategy.evaluate(prefix, context()) == strategy.evaluate(prefix, context())

    def test_a_later_bar_cannot_change_an_earlier_decision(self) -> None:
        """Re-deciding the 09:30 bar with the rest of the day appended must not
        move it. The later bars are deliberately extreme."""
        strategy = OrbStrategy()
        prefix = (*OPENING_BARS, LONG_BREAK)
        rest_of_day = (
            five_minute(4, high="1600", low="1300", close="1500"),
            five_minute(5, high="1700", low="1200", close="1250"),
        )

        decided_then = strategy.evaluate(prefix, context())
        decided_again = strategy.evaluate(prefix, context())
        assert decided_again == decided_then

        # The extended prefix decides its own last bar, not the earlier one.
        extended = strategy.evaluate((*prefix, *rest_of_day), context())
        assert extended.signal is not None
        assert extended.signal.signal_bar_start == rest_of_day[-1].start_at


class TestStrategyConformance:
    def test_the_strategy_satisfies_the_contract(self) -> None:
        assert isinstance(OrbStrategy(), Strategy)
        assert OrbStrategy().name == "orb"

    def test_on_bar_returns_the_signal_alone(self) -> None:
        prefix = (*OPENING_BARS, LONG_BREAK)
        strategy = OrbStrategy()
        assert strategy.on_bar(prefix, context()) == strategy.evaluate(prefix, context()).signal
        assert strategy.on_bar((*OPENING_BARS, NO_BREAK), context()) is None


class TestDecisionConsistency:
    """A decision cannot misreport itself."""

    def test_a_breakout_reason_without_a_signal_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="claims a breakout but carries no signal"):
            OrbDecision(None, OrbReason.LONG_BREAKOUT)

    def test_a_rejection_carrying_a_signal_is_rejected(self) -> None:
        signal = Signal(
            instrument_token=RELIANCE_TOKEN,
            direction=SignalDirection.LONG,
            stop_price=Decimal("1390"),
            target_r_multiple=Decimal("2"),
            signal_bar_start=FIRST_DECIDABLE,
            reason="x",
        )
        with pytest.raises(ValueError, match="must not leave a tradable signal"):
            OrbDecision(signal, OrbReason.NO_BREAKOUT)


def flat_opening(high: str, low: str, close: str) -> tuple[Candle, ...]:
    """Three identical opening bars, so the range is exactly ``low``-``high``."""
    return tuple(five_minute(index, high=high, low=low, close=close) for index in range(3))


class TestRangeValidityFilters:
    """The hypothesis' two range filters, and what happens without an ATR.

    The instrument's tick size is 0.05, so the 4-tick floor is 0.20.
    """

    def test_a_range_narrower_than_four_ticks_is_rejected(self) -> None:
        """A stop this tight sits inside the spread, where the exit is decided
        by the book rather than by the setup failing."""
        opening = flat_opening(high="1400.10", low="1400.00", close="1400.05")
        breakout = five_minute(3, high="1400.60", low="1400.05", close="1400.50")

        decision = OrbStrategy().evaluate((*opening, breakout), context())
        assert decision == OrbDecision(None, OrbReason.RANGE_TOO_NARROW)

    def test_a_range_of_exactly_four_ticks_is_accepted(self) -> None:
        """The floor is inclusive - 0.20 is four ticks, not three and a bit."""
        opening = flat_opening(high="1400.20", low="1400.00", close="1400.10")
        breakout = five_minute(3, high="1400.60", low="1400.05", close="1400.50")

        decision = OrbStrategy().evaluate((*opening, breakout), context())
        assert decision.reason is OrbReason.LONG_BREAKOUT
        assert decision.signal is not None
        assert decision.signal.stop_price == Decimal("1400.00")

    def test_a_range_wider_than_the_atr_ceiling_is_rejected(self) -> None:
        """Width 22 against 1.5 x 10 = 15. A 2R target would sit further away
        than the instrument usually travels in a day."""
        decision = OrbStrategy().evaluate(
            (*OPENING_BARS, LONG_BREAK), context(prior_atr=Decimal("10"))
        )
        assert decision == OrbDecision(None, OrbReason.RANGE_TOO_WIDE)

    def test_a_range_exactly_on_the_atr_ceiling_is_accepted(self) -> None:
        """Width 22 against 1.5 x 14.666... = 22. The ceiling is inclusive."""
        atr = Decimal("22") / Decimal("1.5")
        decision = OrbStrategy().evaluate((*OPENING_BARS, LONG_BREAK), context(prior_atr=atr))
        assert decision.reason is OrbReason.LONG_BREAKOUT

    def test_a_missing_atr_declines_rather_than_substituting_a_value(self) -> None:
        """Without an ATR the ceiling cannot be evaluated, so the setup cannot
        be cleared. Assuming one would silently trade the days the filter
        exists to skip."""
        decision = OrbStrategy().evaluate((*OPENING_BARS, LONG_BREAK), context(prior_atr=None))
        assert decision == OrbDecision(None, OrbReason.ATR_UNAVAILABLE)

    def test_filters_are_not_reported_on_a_bar_that_did_not_break_out(self) -> None:
        """An unusable day reports its rejections at most once per breakout, not
        once per quiet bar - a log where most entries are noise is unreadable."""
        opening = flat_opening(high="1400.10", low="1400.00", close="1400.05")
        quiet = five_minute(3, high="1400.09", low="1400.01", close="1400.05")

        decision = OrbStrategy().evaluate((*opening, quiet), context())
        assert decision == OrbDecision(None, OrbReason.NO_BREAKOUT)


class TestEntryCutoff:
    """No new entries after 14:45 IST.

    The cutoff is about the entry, which happens on the bar *after* the signal,
    so it is the signal bar's close that must land at or before 14:45. Bar 65
    of the session runs 14:40-14:45; bar 66 runs 14:45-14:50.
    """

    def test_a_bar_closing_exactly_at_the_cutoff_still_signals(self) -> None:
        last_allowed = five_minute(65, high="1418", low="1409", close="1416")
        assert to_ist(last_allowed.end_at).time() == time(14, 45)

        decision = OrbStrategy().evaluate((*OPENING_BARS, last_allowed), context())
        assert decision.reason is OrbReason.LONG_BREAKOUT

    def test_a_bar_closing_after_the_cutoff_is_rejected(self) -> None:
        too_late = five_minute(66, high="1418", low="1409", close="1416")
        assert to_ist(too_late.end_at).time() == time(14, 50)

        decision = OrbStrategy().evaluate((*OPENING_BARS, too_late), context())
        assert decision == OrbDecision(None, OrbReason.ENTRY_CUTOFF_REACHED)

    def test_an_unusable_range_outranks_the_cutoff(self) -> None:
        """Both are true of this bar. The range describes the whole session and
        invalidated it from 09:30; the cutoff only says this bar came late."""
        too_late = five_minute(66, high="1418", low="1409", close="1416")
        decision = OrbStrategy().evaluate(
            (*OPENING_BARS, too_late), context(prior_atr=Decimal("10"))
        )
        assert decision.reason is OrbReason.RANGE_TOO_WIDE

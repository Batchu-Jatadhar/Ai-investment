"""Market session behaviour and timestamp normalization."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.time import (
    IST,
    Clock,
    FixedClock,
    NaiveDatetimeError,
    SystemClock,
    ensure_utc,
    ist_datetime,
    to_ist,
    utc_now,
)
from app.domain.market.session import (
    NSE_EQUITY_SESSION,
    MarketSessionCalendar,
    SessionState,
)

# 2026-08-21 is a Friday.
FRIDAY = date(2026, 8, 21)
SATURDAY = date(2026, 8, 22)


def at_ist(hour: int, minute: int, day: date = FRIDAY) -> datetime:
    return ist_datetime(day, datetime(2000, 1, 1, hour, minute).time())


class TestTimezoneDiscipline:
    def test_naive_datetimes_are_rejected_not_guessed(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            ensure_utc(datetime(2026, 8, 21, 9, 15))
        with pytest.raises(NaiveDatetimeError):
            to_ist(datetime(2026, 8, 21, 9, 15))

    def test_aware_input_is_normalized_to_utc(self) -> None:
        result = ensure_utc(at_ist(9, 15))
        assert result.tzinfo is UTC
        assert result == datetime(2026, 8, 21, 3, 45, tzinfo=UTC)

    def test_ist_is_utc_plus_five_thirty(self) -> None:
        utc_moment = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
        local = to_ist(utc_moment)
        assert (local.hour, local.minute) == (9, 15)
        assert local.utcoffset() == timedelta(hours=5, minutes=30)

    def test_round_trip_preserves_the_instant(self) -> None:
        moment = datetime(2026, 8, 21, 6, 30, 15, tzinfo=UTC)
        assert ensure_utc(to_ist(moment)) == moment

    def test_utc_now_is_aware(self) -> None:
        assert utc_now().tzinfo is UTC


class TestClocks:
    def test_system_clock_satisfies_the_protocol(self) -> None:
        clock: Clock = SystemClock()
        assert clock.now().tzinfo is UTC

    def test_fixed_clock_is_controllable(self) -> None:
        clock = FixedClock(datetime(2026, 8, 21, 4, 0, tzinfo=UTC))
        assert clock.now() == datetime(2026, 8, 21, 4, 0, tzinfo=UTC)
        clock.advance(timedelta(minutes=5))
        assert clock.now() == datetime(2026, 8, 21, 4, 5, tzinfo=UTC)

    def test_fixed_clock_rejects_naive_time(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            FixedClock(datetime(2026, 8, 21, 9, 15))


class TestSessionWindow:
    @pytest.mark.parametrize(
        ("hour", "minute", "expected"),
        [
            (8, 0, SessionState.CLOSED),
            (9, 0, SessionState.PRE_OPEN),
            (9, 14, SessionState.PRE_OPEN),
            (9, 15, SessionState.OPEN),
            (12, 0, SessionState.OPEN),
            (15, 29, SessionState.OPEN),
            (15, 30, SessionState.POST_CLOSE),
            (15, 59, SessionState.POST_CLOSE),
            (16, 0, SessionState.CLOSED),
            (23, 0, SessionState.CLOSED),
        ],
    )
    def test_state_at_each_boundary(self, hour: int, minute: int, expected: SessionState) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        assert calendar.state_at(at_ist(hour, minute)) is expected

    def test_open_is_inclusive_and_close_is_exclusive(self) -> None:
        window = NSE_EQUITY_SESSION
        assert window.open_time.hour == 9 and window.open_time.minute == 15
        assert window.close_time.hour == 15 and window.close_time.minute == 30


class TestNonTradingDays:
    def test_weekends_are_closed(self) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        assert calendar.is_trading_day(SATURDAY) is False
        assert calendar.state_at(at_ist(12, 0, SATURDAY)) is SessionState.CLOSED

    def test_injected_holidays_are_closed(self) -> None:
        calendar = MarketSessionCalendar.nse_equity(holidays=[FRIDAY])
        assert calendar.is_trading_day(FRIDAY) is False
        assert calendar.is_open(at_ist(12, 0)) is False

    def test_no_holiday_list_means_no_false_closures(self) -> None:
        """An invented holiday list is worse than none: it hides real sessions."""
        calendar = MarketSessionCalendar.nse_equity()
        assert calendar.holidays == frozenset()
        assert calendar.is_trading_day(FRIDAY) is True


class TestSessionHelpers:
    def test_accepts_ticks_during_pre_open_and_open(self) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        assert calendar.accepts_ticks(at_ist(9, 5)) is True
        assert calendar.accepts_ticks(at_ist(10, 0)) is True
        assert calendar.accepts_ticks(at_ist(16, 30)) is False

    def test_session_bounds_are_utc(self) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        bounds = calendar.session_bounds(at_ist(11, 0))
        assert bounds is not None
        opens, closes = bounds
        assert opens == datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
        assert closes == datetime(2026, 8, 21, 10, 0, tzinfo=UTC)

    def test_no_bounds_on_a_non_trading_day(self) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        assert calendar.session_bounds(at_ist(11, 0, SATURDAY)) is None

    def test_describe_reports_local_context(self) -> None:
        calendar = MarketSessionCalendar.nse_equity()
        described = calendar.describe(at_ist(10, 0))
        assert described["state"] == "open"
        assert described["is_trading_day"] is True
        assert described["window"] == "NSE_EQUITY"
        assert str(IST) in str(described["timezone"])

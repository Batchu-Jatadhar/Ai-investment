"""Exchange-published special sessions: declared weekend trading days.

The one declared NSE equity special session is Sunday 2026-02-01, a full
09:15-15:30 session. Everything else about weekends is unchanged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.time import IST
from app.domain.backtest.engine import run_backtest
from app.domain.backtest.input import BacktestInput
from app.domain.backtest.models import SessionStatus
from app.domain.indicators import session_bars
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval
from app.domain.market.session import (
    NSE_EQUITY_SPECIAL_SESSIONS,
    MarketSessionCalendar,
    SessionState,
)
from app.domain.strategy.params import ORB_V3
from tests.backtest.conftest import make_input
from tests.backtest.test_orb_v3 import RANGES, session_minutes

SPECIAL = date(2026, 2, 1)  # Sunday
NSE = MarketSessionCalendar.nse_equity()
WEEKENDS_ONLY = MarketSessionCalendar.nse_equity(special_sessions=())


def ist(day: date, hh_mm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hh_mm), IST)


class TestCalendar:
    def test_an_ordinary_weekend_is_still_not_trading(self) -> None:
        for day in (date(2026, 1, 31), date(2026, 2, 7), date(2026, 2, 8)):  # Sat, Sat, Sun
            assert not NSE.is_trading_day(day)
            assert NSE.session_bounds(ist(day, "10:00")) is None
            assert NSE.state_at(ist(day, "10:00")) is SessionState.CLOSED

    def test_a_declared_special_sunday_trades_the_normal_window(self) -> None:
        sunday = date(2026, 3, 1)
        calendar = MarketSessionCalendar.nse_equity(special_sessions={sunday})
        assert calendar.is_trading_day(sunday)
        assert calendar.session_bounds(ist(sunday, "10:00")) == (
            ist(sunday, "09:15").astimezone(UTC),
            ist(sunday, "15:30").astimezone(UTC),
        )
        assert calendar.state_at(ist(sunday, "10:00")) is SessionState.OPEN
        assert not calendar.is_trading_day(date(2026, 3, 8))  # the next Sunday is not

    def test_2026_02_01_is_the_declared_nse_equity_special_session(self) -> None:
        assert frozenset({SPECIAL}) == NSE_EQUITY_SPECIAL_SESSIONS
        assert SPECIAL.weekday() == 6
        assert NSE.is_trading_day(SPECIAL)
        assert not WEEKENDS_ONLY.is_trading_day(SPECIAL)

    def test_no_other_weekend_date_trades(self) -> None:
        day, trading_weekends = date(2024, 1, 1), []
        while day <= date(2027, 12, 31):
            if day.weekday() >= 5 and NSE.is_trading_day(day):
                trading_weekends.append(day)
            day += timedelta(days=1)
        assert trading_weekends == [SPECIAL]

    def test_holidays_still_apply_and_cannot_overlap_a_special_session(self) -> None:
        weekday_holiday = date(2026, 1, 26)
        assert not MarketSessionCalendar.nse_equity(holidays={weekday_holiday}).is_trading_day(
            weekday_holiday
        )
        with pytest.raises(ValueError, match="both a holiday and a special session"):
            MarketSessionCalendar.nse_equity(holidays={SPECIAL})


# Fifteen weekday sessions, then the special Sunday carrying the TR-24 range, then
# Monday 2026-02-02 to be decided on. Same hand calculation as ``test_orb_v3``:
# with the Sunday counted the session ATR is 11, without it the seed, 10.
WEEKDAYS = [
    date(2026, 1, 12) + timedelta(days=i)
    for i in range(19)
    if (date(2026, 1, 12) + timedelta(days=i)).weekday() < 5
]
SESSIONS = [*WEEKDAYS[:15], SPECIAL]
MONDAY = date(2026, 2, 2)


def minutes() -> list[Candle]:
    history = [
        m
        for day, (h, lo) in zip(SESSIONS, RANGES, strict=True)
        for m in session_minutes(day, h, lo)
    ]
    return history + session_minutes(MONDAY, "1003.00", "997.00", until=time(10, 0))


def build(calendar: MarketSessionCalendar, bars: list[Candle] | None = None) -> BacktestInput:
    series = sorted(bars if bars is not None else minutes(), key=lambda m: m.start_at)
    return make_input(
        candles_5m=aggregate_minutes(series, CandleInterval.M5).candles,
        candles_1m=tuple(series),
        calendar=calendar,
        strategy_params=ORB_V3,
    )


class TestV3AndTheEngine:
    def test_the_fixture_is_fifteen_weekdays_then_the_special_sunday(self) -> None:
        assert len(WEEKDAYS[:15]) == 15 and WEEKDAYS[14] == date(2026, 1, 30)
        assert all(day.weekday() < 5 for day in WEEKDAYS[:15])

    def test_the_special_session_becomes_a_session_bar(self) -> None:
        with_special = session_bars(minutes(), NSE, window_end=time(15, 15))
        without = session_bars(minutes(), WEEKENDS_ONLY, window_end=time(15, 15))
        assert SPECIAL in [b.session for b in with_special]
        assert SPECIAL not in [b.session for b in without]

    def test_v3_session_atr_incorporates_the_special_session(self) -> None:
        assert build(NSE).prior_atr(MONDAY) == Decimal("11")
        assert build(WEEKENDS_ONLY).prior_atr(MONDAY) == Decimal("10")

    def test_the_engine_processes_and_logs_the_special_session(self) -> None:
        result = run_backtest(
            build(NSE),
            starting_capital=Decimal("500000"),
            generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        (record,) = [r for r in result.session_log if r.session == SPECIAL]
        assert record.status is SessionStatus.NO_SIGNAL
        assert [r.session for r in result.session_log] == [*SESSIONS, MONDAY]

    def test_without_the_declaration_the_engine_still_refuses_sunday_bars(self) -> None:
        with pytest.raises(ValueError, match="not a trading day on the calendar"):
            run_backtest(
                build(WEEKENDS_ONLY),
                starting_capital=Decimal("500000"),
                generated_at=datetime(2026, 9, 1, tzinfo=UTC),
            )


class TestFingerprints:
    def test_declaring_the_session_changes_fingerprints_that_include_the_date(self) -> None:
        with_special, without = build(NSE), build(WEEKENDS_ONLY)
        assert with_special.fingerprint() != without.fingerprint()
        assert with_special.canonical_payload()["calendar"]["special_sessions"] == "2026-02-01"  # type: ignore[index]
        assert "special_sessions" not in without.canonical_payload()["calendar"]  # type: ignore[operator]

    def test_inputs_outside_the_date_keep_their_fingerprint(self) -> None:
        later = [m for m in minutes() if m.start_at >= ist(date(2026, 1, 19), "00:00")]
        early = [m for m in minutes() if m.start_at < ist(date(2026, 1, 31), "00:00")]
        assert build(NSE, early).fingerprint() == build(WEEKENDS_ONLY, early).fingerprint()
        assert "special_sessions" not in build(NSE, early).canonical_payload()["calendar"]  # type: ignore[operator]
        assert build(NSE, later).fingerprint() != build(WEEKENDS_ONLY, later).fingerprint()

    def test_the_fingerprint_is_deterministic(self) -> None:
        assert build(NSE).fingerprint() == build(NSE).fingerprint()

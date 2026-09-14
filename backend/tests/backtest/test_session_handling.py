"""Sessions without a valid opening range are untradable, not fatal.

Built on the golden run's days. Each scenario changes only Thursday 2026-08-20,
the golden no-signal day, and keeps every replacement bar's true range at 10.00,
so Friday's prior ATR stays exactly 10 and Friday's trade must come out exactly
as it does in the golden run.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.domain.backtest.engine import run_backtest
from app.domain.backtest.models import SessionRecord, SessionStatus
from app.domain.backtest.result import BacktestResult
from app.domain.indicators import IndicatorError
from app.domain.market.models import Candle
from app.domain.strategy.contract import Signal, StrategyContext
from app.domain.strategy.orb import OrbStrategy
from app.domain.strategy.params import OrbParams
from tests.backtest.conftest import make_candle
from tests.backtest.test_golden_backtest import (
    CAPITAL,
    FRI,
    FRI_5M,
    GENERATED_AT,
    M1,
    MON,
    MON_5M,
    THU,
    WED,
    WED_5M,
    at,
    golden_input,
    run_golden,
    session_bars,
)

FLAT = ("1000.00", "1005.00", "995.00", "1000.00")  # true range 10 from a 1000 close

#: A one-hour special session, 13:45-14:45, with no 09:15 opening window.
SPECIAL_THU = session_bars(
    THU, [(f"{13 + (45 + 5 * i) // 60}:{(45 + 5 * i) % 60:02d}", *FLAT) for i in range(12)]
)
#: A normal session missing its 09:20 bar, with a close above the would-be range
#: afterwards that must not become a signal.
HOLED_THU = session_bars(
    THU,
    [
        ("09:15", *FLAT),
        ("09:25", *FLAT),
        ("09:30", *FLAT),
        ("09:35", "1000.00", "1010.00", "1000.00", "1010.00"),
    ],
)


def run_with_thursday(thursday: tuple[Candle, ...], **kwargs: object) -> BacktestResult:
    minutes = tuple(
        make_candle(bars[0].start_at, M1) for bars in (WED_5M, thursday, FRI_5M, MON_5M) if bars
    )
    data = golden_input(candles_5m=WED_5M + thursday + FRI_5M + MON_5M, candles_1m=minutes)
    return run_backtest(data, starting_capital=CAPITAL, generated_at=GENERATED_AT, **kwargs)  # type: ignore[arg-type]


def statuses(result: BacktestResult) -> list[tuple[str, SessionStatus]]:
    return [(r.session.isoformat(), r.status) for r in result.session_log]


class Recording:
    """The real strategy, noting every session it is asked about."""

    def __init__(self) -> None:
        self.inner = OrbStrategy(OrbParams())
        self.name, self.version = self.inner.name, self.inner.version
        self.sessions: list[str] = []

    def on_bar(self, session_bars: Sequence[Candle], context: StrategyContext) -> Signal | None:
        self.sessions.append(context.session_date.isoformat())
        return self.inner.on_bar(session_bars, context)


GOLDEN = run_golden()


class TestUntradableSessions:
    @pytest.mark.parametrize(
        "thursday", [SPECIAL_THU, HOLED_THU], ids=["special-session", "holed-open"]
    )
    def test_the_strategy_alone_cannot_form_a_range_there(
        self, thursday: tuple[Candle, ...]
    ) -> None:
        """What the engine now protects the run from: the strategy's own contract
        is to raise on a session whose opening window is not covered."""
        bounds = golden_input().calendar.session_bounds(THU)
        assert bounds is not None
        context = StrategyContext(
            instrument=golden_input().instrument,
            calendar=golden_input().calendar,
            session_open=bounds[0],
            session_close=bounds[1],
            prior_atr=None,
        )
        with pytest.raises(IndicatorError):
            OrbStrategy().evaluate(thursday, context)

    @pytest.mark.parametrize(
        "thursday", [SPECIAL_THU, HOLED_THU], ids=["special-session", "holed-open"]
    )
    def test_the_session_is_untradable_and_the_run_continues(
        self, thursday: tuple[Candle, ...]
    ) -> None:
        result = run_with_thursday(thursday)
        assert statuses(result) == [
            ("2026-08-19", SessionStatus.NO_SIGNAL),
            ("2026-08-20", SessionStatus.UNTRADABLE_NO_OPENING_RANGE),
            ("2026-08-21", SessionStatus.TRADED),
            ("2026-08-24", SessionStatus.SIGNALLED),
        ]

    @pytest.mark.parametrize(
        "thursday", [SPECIAL_THU, HOLED_THU], ids=["special-session", "holed-open"]
    )
    def test_the_valid_session_right_after_trades_exactly_as_in_the_golden_run(
        self, thursday: tuple[Candle, ...]
    ) -> None:
        result = run_with_thursday(thursday)
        assert result.trades == GOLDEN.trades
        assert [r.signal for r in result.signal_log] == [r.signal for r in GOLDEN.signal_log]

    @pytest.mark.parametrize(
        "thursday", [SPECIAL_THU, HOLED_THU], ids=["special-session", "holed-open"]
    )
    def test_nothing_is_fabricated_for_the_untradable_session(
        self, thursday: tuple[Candle, ...]
    ) -> None:
        recording = Recording()
        result = run_with_thursday(thursday, strategy=recording)
        assert "2026-08-20" not in recording.sessions  # never asked
        assert not any(r.signal.signal_bar_start.date() == THU.date() for r in result.signal_log)
        assert not any(t.entry.occurred_at.date() == THU.date() for t in result.trades)
        (thursday_record,) = [r for r in result.session_log if r.session == THU.date()]
        assert (thursday_record.signal_count, thursday_record.trade_count) == (0, 0)


class TestReporting:
    def test_the_golden_run_records_each_valid_session_outcome(self) -> None:
        assert statuses(GOLDEN) == [
            ("2026-08-19", SessionStatus.NO_SIGNAL),
            ("2026-08-20", SessionStatus.NO_SIGNAL),
            ("2026-08-21", SessionStatus.TRADED),
            ("2026-08-24", SessionStatus.SIGNALLED),
        ]
        summary = GOLDEN.canonical()
        assert (
            summary["sessions_no_data"],
            summary["sessions_untradable_no_opening_range"],
            summary["sessions_no_signal"],
            summary["sessions_signalled"],
            summary["sessions_traded"],
        ) == ("0", "0", "2", "1", "1")

    def test_a_trading_day_with_no_bars_is_no_data_and_weekends_are_not_listed(self) -> None:
        result = run_with_thursday(())
        assert statuses(result) == [
            ("2026-08-19", SessionStatus.NO_SIGNAL),
            ("2026-08-20", SessionStatus.NO_DATA),
            ("2026-08-21", SessionStatus.TRADED),
            ("2026-08-24", SessionStatus.SIGNALLED),
        ]
        assert result.trades == GOLDEN.trades  # 15 Wednesday bars still give Friday an ATR

    def test_the_four_kinds_of_session_are_distinguished_in_the_summary(self) -> None:
        summary = run_with_thursday(SPECIAL_THU).canonical()
        assert summary["sessions_untradable_no_opening_range"] == "1"
        assert summary["sessions_no_data"] == "0"
        assert run_with_thursday(()).canonical()["sessions_no_data"] == "1"

    @pytest.mark.parametrize(
        "thursday", [SPECIAL_THU, HOLED_THU, ()], ids=["special", "holed", "no-data"]
    )
    def test_reporting_is_deterministic(self, thursday: tuple[Candle, ...]) -> None:
        first, second = run_with_thursday(thursday), run_with_thursday(thursday)
        assert first.session_log == second.session_log
        assert first.canonical() == second.canonical()
        assert first.manifest.input_fingerprint == second.manifest.input_fingerprint


class TestSessionRecord:
    @pytest.mark.parametrize(
        ("status", "signals", "trades"),
        [
            (SessionStatus.TRADED, 1, 0),
            (SessionStatus.SIGNALLED, 1, 1),
            (SessionStatus.NO_SIGNAL, 1, 0),
            (SessionStatus.UNTRADABLE_NO_OPENING_RANGE, 1, 0),
            (SessionStatus.NO_DATA, 0, 1),
        ],
    )
    def test_a_status_must_agree_with_its_counts(
        self, status: SessionStatus, signals: int, trades: int
    ) -> None:
        with pytest.raises(ValueError):
            SessionRecord(THU.date(), status, signal_count=signals, trade_count=trades)


def test_the_fixture_dates_are_what_the_scenarios_assume() -> None:
    assert [d.date().isoformat() for d in (WED, THU, FRI, MON)] == [
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-24",
    ]
    assert at(THU, "13:45") == SPECIAL_THU[0].start_at

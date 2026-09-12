"""Execution edge cases, run end to end through the engine on tiny synthetic days.

Every scenario reuses the Phase 2.6.1 fixture's first two days (see
``test_engine.py``): the 15-bar no-trade day supplies ATR 10, and the trade
day's 09:15-09:35 bars form the opening range 990.00-1002.00 and fire a LONG on
the 09:35 close. From there each test supplies its own bars. The long always
enters on the 09:40 bar, opening 999.95:

    entry   999.95 + 1 tick = 1000.00 x 100 (BUY), costs 30.34
    stop    990.00, filled 1 tick worse at 989.95 on a touch
    target  1000.00 + 2 x 10.00 = 1020.00, trigger 1020.05, filled at 1019.95

.. rubric:: Same-bar collision - the 09:45 bar spans 989.00-1021.00

It reaches the stop and trades through the target, so its 1-minute bars decide.

    stop first     gross (989.95 - 1000.00) x 100              = -1,005.00
                   exit turnover 98,995.00: brokerage 20.00 (capped),
                   STT 24.74875 -> 24.75, exchange 3.0391465 -> 3.04,
                   SEBI 0.10, GST 18% x 23.14 = 4.1652 -> 4.17  =     52.06
                   costs 30.34 + 52.06                          =     82.40
                   net -1005.00 - 82.40                         = -1,087.40
    target first   gross 1,995.00, costs 83.25, net 1,911.75 (as in test_engine)

.. rubric:: Gap through the stop - the 09:45 bar opens at 985.00, below 990.00

    fill           the open, 985.00, less 1 tick                =    984.95
    gross          (984.95 - 1000.00) x 100                     = -1,505.00
    exit costs     turnover 98,495.00: brokerage 20.00 (capped),
                   STT 24.62375 -> 24.62, exchange 3.0237965 -> 3.02,
                   SEBI 0.10, GST 18% x 23.12 = 4.1616 -> 4.16  =     51.90
    costs          30.34 + 51.90                                =     82.24
    net            -1505.00 - 82.24                             = -1,587.24
    ending equity  500,000.00 - 1,587.24                        = 498,412.76
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest import engine
from app.domain.backtest.config import SlippageConfig
from app.domain.backtest.execution import (
    ExecutionIntent,
    ExecutionStatus,
    UnexecutableBarError,
    resolve_entry_fill,
)
from app.domain.backtest.models import AmbiguityResolution, FillReason
from app.domain.backtest.result import BacktestResult
from app.domain.market.models import Candle, CandleInterval
from app.domain.strategy.contract import SignalDirection
from tests.backtest.conftest import make_candle
from tests.backtest.test_engine import (
    NO_TRADE_1M,
    NO_TRADE_5M,
    TRADE_1M,
    TRADE_5M,
    TRADE_OPEN,
    bars,
    ist,
    run,
    synthetic_input,
)

M1, M5 = CandleInterval.M1, CandleInterval.M5

#: Opening range and the 09:35 LONG signal.
SETUP_5M = TRADE_5M[:5]
ENTRY_5M = bars(TRADE_OPEN, M5, [("09:40", "999.95", "1008.00", "998.00", "1006.00")])
#: A minute the trade day always has, so the input's 1m coverage check passes.
COVERAGE_1M = bars(TRADE_OPEN, M1, [("09:15", "995.00", "1000.00", "990.00", "998.00")])
COLLISION_5M = bars(TRADE_OPEN, M5, [("09:45", "1006.00", "1021.00", "989.00", "995.00")])

STOP_FIRST_1M = bars(
    TRADE_OPEN,
    M1,
    [
        ("09:45", "1006.00", "1007.00", "995.00", "996.00"),
        ("09:46", "996.00", "998.00", "989.00", "991.00"),  # stop touched
        ("09:47", "991.00", "1010.00", "990.50", "1009.00"),
        ("09:48", "1009.00", "1021.00", "1008.00", "1015.00"),  # target, too late
        ("09:49", "1015.00", "1016.00", "994.00", "995.00"),
    ],
)
TARGET_FIRST_1M = bars(
    TRADE_OPEN,
    M1,
    [
        ("09:45", "1006.00", "1012.00", "1005.00", "1011.00"),
        ("09:46", "1011.00", "1021.00", "1010.00", "1018.00"),  # target traded through
        ("09:47", "1018.00", "1019.00", "989.00", "992.00"),  # stop, too late
        ("09:48", "992.00", "996.00", "990.50", "994.00"),
        ("09:49", "994.00", "997.00", "993.00", "995.00"),
    ],
)
#: A later 5m bar and its first minute: present in the input, never consulted
#: while the 09:45 collision is being resolved.
LATER_5M = bars(TRADE_OPEN, M5, [("09:50", "995.00", "999.00", "994.00", "998.00")])
LATER_1M = bars(TRADE_OPEN, M1, [("09:50", "995.00", "1030.00", "994.00", "998.00")])


def trade_day(after_signal_5m: tuple[Candle, ...], minutes: tuple[Candle, ...]) -> BacktestResult:
    """History day, then the trade day's setup followed by ``after_signal_5m``."""
    return run(
        synthetic_input(
            NO_TRADE_5M + SETUP_5M + after_signal_5m,
            NO_TRADE_1M + COVERAGE_1M + minutes,
        )
    )


class TestSameBarExitResolution:
    def test_stop_before_target_is_resolved_by_the_minute_bars(self) -> None:
        result = trade_day(ENTRY_5M + COLLISION_5M, STOP_FIRST_1M)
        (trade,) = result.trades
        assert trade.exit_reason is FillReason.STOP
        assert trade.ambiguity is AmbiguityResolution.RESOLVED_BY_1M
        assert trade.exit.price == Decimal("989.95")
        assert trade.exit.reference_price == Decimal("990.00")
        assert trade.exit.occurred_at == ist(TRADE_OPEN, "09:50")  # the bar's close
        assert trade.exit.costs == Decimal("52.06")
        assert (trade.gross_pnl, trade.costs, trade.net_pnl) == (
            Decimal("-1005.00"),
            Decimal("82.40"),
            Decimal("-1087.40"),
        )
        assert result.performance is not None
        assert result.performance.ambiguous_exit_count == 1
        assert result.performance.pessimistic_fallback_count == 0

    def test_target_before_stop_is_resolved_by_the_minute_bars(self) -> None:
        result = trade_day(ENTRY_5M + COLLISION_5M, TARGET_FIRST_1M)
        (trade,) = result.trades
        assert trade.exit_reason is FillReason.TARGET
        assert trade.ambiguity is AmbiguityResolution.RESOLVED_BY_1M
        assert trade.exit.price == Decimal("1019.95")
        assert (trade.gross_pnl, trade.costs, trade.net_pnl) == (
            Decimal("1995.00"),
            Decimal("83.25"),
            Decimal("1911.75"),
        )
        assert result.performance is not None
        assert result.performance.ambiguous_exit_count == 1
        assert result.performance.pessimistic_fallback_count == 0

    def test_missing_minute_bars_fall_back_to_the_stop(self) -> None:
        """Only four of the five minutes exist. The first of them shows the
        target traded through, and it still does not decide: partial coverage is
        no coverage, so the stop is assumed."""
        result = trade_day(ENTRY_5M + COLLISION_5M, TARGET_FIRST_1M[:4])
        (trade,) = result.trades
        assert trade.exit_reason is FillReason.STOP
        assert trade.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        assert trade.exit.price == Decimal("989.95")
        assert trade.net_pnl == Decimal("-1087.40")
        assert result.performance is not None
        assert result.performance.ambiguous_exit_count == 1
        assert result.performance.pessimistic_fallback_count == 1


def test_execution_reads_only_the_next_bar_and_its_own_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries: list[datetime] = []
    exits: list[tuple[datetime, tuple[Candle, ...]]] = []
    original_entry, original_exit = engine.resolve_entry_fill, engine.resolve_exit_fill

    def entry_spy(intent, bar, **kwargs):  # noqa: ANN001, ANN202
        entries.append(bar.start_at)
        return original_entry(intent, bar, **kwargs)

    def exit_spy(intent, entry, bar, **kwargs):  # noqa: ANN001, ANN202
        exits.append((bar.start_at, kwargs["minute_bars"]))
        return original_exit(intent, entry, bar, **kwargs)

    monkeypatch.setattr(engine, "resolve_entry_fill", entry_spy)
    monkeypatch.setattr(engine, "resolve_exit_fill", exit_spy)

    result = trade_day(ENTRY_5M + COLLISION_5M + LATER_5M, STOP_FIRST_1M + LATER_1M)

    assert result.trades[0].exit_reason is FillReason.STOP
    # The 09:35 signal is entered on 09:40 and nothing else (a sizing probe,
    # then the fill).
    assert entries == [ist(TRADE_OPEN, "09:40")] * 2
    # Each exit check sees its own bar's minutes only - not the 09:50 minute
    # that spikes to 1030 - and nothing is checked once the position is closed.
    assert exits == [
        (ist(TRADE_OPEN, "09:40"), ()),
        (ist(TRADE_OPEN, "09:45"), STOP_FIRST_1M),
    ]


GAP_5M = bars(TRADE_OPEN, M5, [("09:45", "985.00", "987.00", "980.00", "982.00")])


class TestGapsAndMissingExecution:
    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self) -> None:
        result = trade_day(ENTRY_5M + GAP_5M, ())
        (trade,) = result.trades
        assert trade.exit_reason is FillReason.STOP
        assert trade.ambiguity is AmbiguityResolution.UNAMBIGUOUS
        assert trade.exit.reference_price == Decimal("985.00")
        assert trade.exit.price == Decimal("984.95")
        assert trade.exit.occurred_at == ist(TRADE_OPEN, "09:45")  # the opening print
        assert trade.exit.costs == Decimal("51.90")
        assert (trade.gross_pnl, trade.costs, trade.net_pnl) == (
            Decimal("-1505.00"),
            Decimal("82.24"),
            Decimal("-1587.24"),
        )
        assert result.equity_curve[-1].equity == Decimal("498412.76")

    def test_a_signal_with_no_bar_after_it_is_logged_and_never_traded(self) -> None:
        """The gap bar also closes below the range: a SHORT on the session's last
        bar. It stays in the log, and no second trade is fabricated for it."""
        result = trade_day(ENTRY_5M + GAP_5M, ())
        assert [(r.signal.direction, r.signal.signal_bar_start) for r in result.signal_log] == [
            (SignalDirection.LONG, ist(TRADE_OPEN, "09:35")),
            (SignalDirection.SHORT, ist(TRADE_OPEN, "09:45")),
        ]
        assert len(result.trades) == 1

        # The explicit outcome for that signal, from the resolver the engine uses.
        short = result.signal_log[1].signal
        intent = ExecutionIntent(short, 1, ist(TRADE_OPEN, "09:50"))
        outcome = resolve_entry_fill(
            intent, None, tick_size=Decimal("0.05"), slippage=SlippageConfig()
        )
        assert (outcome.status, outcome.fill) == (ExecutionStatus.NO_EXECUTION_BAR, None)

    @pytest.mark.parametrize(
        ("after_signal", "status", "bar_at"),
        [
            pytest.param(
                (make_candle(ist(TRADE_OPEN, "09:40"), M5, open_="999.95", volume=0),),
                ExecutionStatus.NO_VOLUME,
                "09:40",
                id="entry-bar-without-volume",
            ),
            pytest.param(
                ENTRY_5M
                + (
                    make_candle(
                        ist(TRADE_OPEN, "09:45"),
                        M5,
                        open_="1006.00",
                        high="1006.00",
                        low="1006.00",
                        close="1006.00",
                        volume=0,
                    ),
                ),
                ExecutionStatus.NO_RANGE,
                "09:45",
                id="held-bar-that-never-traded",
            ),
        ],
    )
    def test_an_unexecutable_bar_stops_the_run_explicitly(
        self, after_signal: tuple[Candle, ...], status: ExecutionStatus, bar_at: str
    ) -> None:
        messages = []
        for _ in range(2):
            with pytest.raises(UnexecutableBarError) as raised:
                trade_day(after_signal, ())
            assert raised.value.status is status
            assert raised.value.bar_start == ist(TRADE_OPEN, bar_at)
            messages.append(str(raised.value))
        assert messages[0] == messages[1]

    def test_a_position_still_open_when_the_session_runs_out_is_an_error(self) -> None:
        """The day's bars stop at 09:40, long before the 15:15 hard exit. The
        engine refuses rather than inventing a closing price."""
        with pytest.raises(ValueError, match="still held when its bars ran out"):
            trade_day(ENTRY_5M, ())


def three_days_later(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    """Friday's bars moved to Monday 2026-08-24, prices untouched."""
    shift = timedelta(days=3)
    return tuple(replace(c, start_at=c.start_at + shift, end_at=c.end_at + shift) for c in candles)


MONDAY_5M, MONDAY_1M = three_days_later(TRADE_5M), three_days_later(TRADE_1M)


class TestMultiSession:
    """Thursday is ATR history. Friday signals on its last bar and cannot
    execute. Monday replays the full Phase 2.6.1 trade session.

    Monday's ATR now includes Friday's five bars: true ranges 16, 8, 8, 4.5 and
    4 fold 10 down to about 9.29, and 1.5 x 9.29 is still wider than the 12.00
    opening range, so Monday trades exactly as the original fixture did."""

    RUN = synthetic_input(
        NO_TRADE_5M + SETUP_5M + MONDAY_5M,
        NO_TRADE_1M + COVERAGE_1M + MONDAY_1M,
    )
    MONDAY = ist(TRADE_OPEN, "09:15") + timedelta(days=3)

    def test_sessions_aggregate_and_nothing_leaks_between_them(self) -> None:
        result = run(self.RUN)

        (trade,) = result.trades
        # Friday's unexecuted signal is not carried into Monday's 09:15 bar.
        assert trade.entry.occurred_at == self.MONDAY + timedelta(minutes=25)  # 09:40
        assert trade.exit.occurred_at == self.MONDAY + timedelta(minutes=40)  # 09:55
        assert (trade.entry.price, trade.exit.price, trade.entry.quantity) == (
            Decimal("1000.00"),
            Decimal("1019.95"),
            100,
        )
        assert trade.net_pnl == Decimal("1911.75")
        assert [(p.at, p.equity) for p in result.equity_curve] == [
            (ist(TRADE_OPEN, "09:15") - timedelta(days=1), Decimal("500000")),
            (trade.exit.occurred_at, Decimal("501911.75")),
        ]
        assert result.performance is not None
        assert result.performance.portfolio.net_pnl == Decimal("1911.75")
        assert result.performance.portfolio.active_days == 1

    def test_signal_log_keeps_the_unexecuted_signal_and_trades_hold_only_fills(self) -> None:
        result = run(self.RUN)
        assert [r.signal.signal_bar_start for r in result.signal_log] == [
            ist(TRADE_OPEN, "09:35"),
            self.MONDAY + timedelta(minutes=20),
        ]
        assert all(r.accepted for r in result.signal_log)
        assert [t.entry.occurred_at for t in result.trades] == [self.MONDAY + timedelta(minutes=25)]


#: 09:40 is missing. These later bars would have made a winning long had the
#: engine entered on the first bar it happened to have.
AFTER_A_MISSING_BUCKET_5M = bars(
    TRADE_OPEN,
    M5,
    [
        ("09:45", "999.95", "1008.00", "998.00", "1006.00"),
        ("09:50", "1006.00", "1021.00", "1004.00", "1018.00"),
    ],
)


class TestNextBarEntry:
    def test_entry_is_the_bucket_immediately_after_the_signal_bar(self) -> None:
        result = trade_day(ENTRY_5M + COLLISION_5M, STOP_FIRST_1M)
        (record,) = result.signal_log
        assert result.trades[0].entry.bar_start == record.signal.signal_bar_start + M5.delta

    def test_a_missing_next_bucket_means_no_entry(self) -> None:
        result = trade_day(AFTER_A_MISSING_BUCKET_5M[:1], ())
        assert [r.signal.signal_bar_start for r in result.signal_log] == [ist(TRADE_OPEN, "09:35")]
        assert result.trades == ()
        assert result.equity_curve[-1].equity == Decimal("500000")

    def test_a_later_bar_is_never_used_in_its_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        offered: list[Candle | None] = []
        original = engine.resolve_entry_fill

        def spy(intent, next_bar, **kwargs):  # noqa: ANN001, ANN202
            offered.append(next_bar)
            return original(intent, next_bar, **kwargs)

        monkeypatch.setattr(engine, "resolve_entry_fill", spy)
        result = trade_day(AFTER_A_MISSING_BUCKET_5M, ())

        assert offered == [None]  # asked once, with no bar - not with 09:45
        assert result.trades == ()
        assert len(result.signal_log) == 1

    def test_the_outcome_is_deterministic(self) -> None:
        first = trade_day(AFTER_A_MISSING_BUCKET_5M, ())
        second = trade_day(AFTER_A_MISSING_BUCKET_5M, ())
        assert json.dumps(asdict(first), default=str, sort_keys=True) == json.dumps(
            asdict(second), default=str, sort_keys=True
        )

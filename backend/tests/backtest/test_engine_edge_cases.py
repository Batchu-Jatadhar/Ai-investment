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
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.domain.backtest import engine
from app.domain.backtest.models import AmbiguityResolution, FillReason
from app.domain.backtest.result import BacktestResult
from app.domain.market.models import Candle, CandleInterval
from tests.backtest.test_engine import (
    NO_TRADE_1M,
    NO_TRADE_5M,
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

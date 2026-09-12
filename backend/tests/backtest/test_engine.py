"""End-to-end engine tests on a hand-designed synthetic session.

bars -> ORB signal -> execution -> fill -> trade -> portfolio -> costs ->
metrics -> BacktestResult, with every expected number worked out by hand below
and written as a constant. Nothing here computes an expectation with production
code.

.. rubric:: The trade session - Friday 2026-08-21 (IST), RELIANCE, tick 0.05

    5m bar   open     high     low      close
    09:15    995.00   1000.00  990.00   998.00   opening range
    09:20    998.00   1002.00  994.00   996.00   opening range
    09:25    996.00   1001.00  993.00   999.00   opening range
    09:30    999.00   1001.50  997.00   1001.00  close 1001 <= 1002: NO_BREAKOUT
    09:35    1001.00  1004.00  1000.00  1003.00  close 1003 > 1002: LONG signal
    09:40    999.95   1008.00  998.00   1006.00  entry bar
    09:45    1006.00  1015.00  1004.00  1012.00  no exit
    09:50    1012.00  1021.00  1010.00  1018.00  target traded through
    09:55    1018.00  1019.00  1015.00  1016.00  flat; long already signalled

    opening range   high 1002.00, low 990.00, width 12.00
                    >= 4 ticks (0.20); <= 1.5 x prior ATR 10.00 (15.00)
    prior ATR       Wilder ATR(14) over the 15 bars of the day before, whose
                    14 true ranges are all exactly 10.00          = 10.00
    signal          LONG on the 09:35 bar, stop 990.00 (range low), target 2R
    entry           09:40 open 999.95 + 1 tick slippage = 1000.00 (BUY)
    quantity        floor(100000 / 1000.00)                       = 100
    risk            1000.00 - 990.00                              = 10.00
    target          1000.00 + 2 x 10.00                           = 1020.00
    trigger         1020.00 + 1 tick through                      = 1020.05
    exit            09:50 high 1021.00 >= 1020.05, stop 990 untouched
                    fill 1020.00 - 1 tick = 1019.95 (SELL), at bar close 09:55

    entry costs     turnover 100 x 1000.00 = 100,000.00
                    brokerage 0.03% = 30.00, capped          20.00
                    stamp duty 0.003%                         3.00
                    exchange 0.00307%                         3.07
                    SEBI 0.0001%                              0.10
                    GST 18% x (20.00 + 0.10 + 3.07) = 4.1706  4.17
                    no STT on a buy                   total  30.34

    exit costs      turnover 100 x 1019.95 = 101,995.00
                    brokerage 0.03% = 30.5985, capped        20.00
                    STT 0.025% = 25.49875                    25.50
                    exchange 0.00307% = 3.1312465             3.13
                    SEBI 0.0001% = 0.101995                   0.10
                    GST 18% x (20.00 + 0.10 + 3.13) = 4.1814  4.18
                    no stamp duty on a sell           total  52.91

    gross           (1019.95 - 1000.00) x 100                     = 1,995.00
    costs           30.34 + 52.91                                 =    83.25
    net             1995.00 - 83.25                               = 1,911.75
    ending cash     500,000.00 + 1,911.75                         = 501,911.75
    total return    1911.75 / 500000                              = 0.0038235
    holding         09:40 -> 09:55                                = 15 min
    exposure        900 s held / 88,800 s curve span (08-20 09:15 to 08-21 09:55)
                    = 3/296, to 28 significant digits
                                              = 0.01013513513513513513513513514

.. rubric:: The no-trade session - Thursday 2026-08-20 (IST)

It runs first and is the trade session's ATR history. Opening range
1000.00-1010.00 from three A bars. Then B and C alternate; B wicks to 1012 and
C to 998, but every close stays inside the range, so nothing signals - and with
no earlier session it has no ATR anyway.

    A  1005.00  1010.00  1000.00  1005.00
    B  1005.00  1012.00  1002.00  1004.00   after close 1005 or 1006: TR 10.00
    C  1004.00  1008.00   998.00  1006.00   after close 1004:         TR 10.00
    A after A: TR 10.00. Every one of the 14 true ranges is 10.00, so ATR = 10.00.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest import engine
from app.domain.backtest.config import NSE_INTRADAY_EQUITY
from app.domain.backtest.engine import run_backtest
from app.domain.backtest.input import BacktestInput
from app.domain.backtest.models import FillReason, OrderSide
from app.domain.backtest.result import BacktestResult
from app.domain.market.models import Candle, CandleInterval
from app.domain.strategy.contract import SignalDirection, StrategyContext
from app.domain.strategy.orb import OrbStrategy
from tests.backtest.conftest import make_candle, make_input

CAPITAL = Decimal("500000")
GENERATED_AT = datetime(2026, 9, 1, tzinfo=UTC)

#: 09:15 IST on each session, in UTC.
NO_TRADE_OPEN = datetime(2026, 8, 20, 3, 45, tzinfo=UTC)
TRADE_OPEN = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


def ist(open_at: datetime, hh_mm: str) -> datetime:
    hours, minutes = (int(part) for part in hh_mm.split(":"))
    return open_at + timedelta(hours=hours - 9, minutes=minutes - 15)


def bars(
    open_at: datetime, interval: CandleInterval, rows: list[tuple[str, str, str, str, str]]
) -> tuple[Candle, ...]:
    return tuple(
        make_candle(ist(open_at, at), interval, open_=o, high=h, low=lo, close=c)
        for at, o, h, lo, c in rows
    )


TRADE_5M = bars(
    TRADE_OPEN,
    CandleInterval.M5,
    [
        ("09:15", "995.00", "1000.00", "990.00", "998.00"),
        ("09:20", "998.00", "1002.00", "994.00", "996.00"),
        ("09:25", "996.00", "1001.00", "993.00", "999.00"),
        ("09:30", "999.00", "1001.50", "997.00", "1001.00"),
        ("09:35", "1001.00", "1004.00", "1000.00", "1003.00"),
        ("09:40", "999.95", "1008.00", "998.00", "1006.00"),
        ("09:45", "1006.00", "1015.00", "1004.00", "1012.00"),
        ("09:50", "1012.00", "1021.00", "1010.00", "1018.00"),
        ("09:55", "1018.00", "1019.00", "1015.00", "1016.00"),
    ],
)
#: The minutes inside the 09:50 exit bar.
TRADE_1M = bars(
    TRADE_OPEN,
    CandleInterval.M1,
    [
        ("09:50", "1012.00", "1014.00", "1010.00", "1013.00"),
        ("09:51", "1013.00", "1016.00", "1012.00", "1015.00"),
        ("09:52", "1015.00", "1021.00", "1014.00", "1019.00"),
        ("09:53", "1019.00", "1020.00", "1017.00", "1018.50"),
        ("09:54", "1018.50", "1019.00", "1017.50", "1018.00"),
    ],
)
NO_TRADE_5M = bars(
    NO_TRADE_OPEN,
    CandleInterval.M5,
    [
        ("09:15", "1005.00", "1010.00", "1000.00", "1005.00"),
        ("09:20", "1005.00", "1010.00", "1000.00", "1005.00"),
        ("09:25", "1005.00", "1010.00", "1000.00", "1005.00"),
        ("09:30", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("09:35", "1004.00", "1008.00", "998.00", "1006.00"),
        ("09:40", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("09:45", "1004.00", "1008.00", "998.00", "1006.00"),
        ("09:50", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("09:55", "1004.00", "1008.00", "998.00", "1006.00"),
        ("10:00", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("10:05", "1004.00", "1008.00", "998.00", "1006.00"),
        ("10:10", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("10:15", "1004.00", "1008.00", "998.00", "1006.00"),
        ("10:20", "1005.00", "1012.00", "1002.00", "1004.00"),
        ("10:25", "1004.00", "1008.00", "998.00", "1006.00"),
    ],
)
#: The minutes inside the 09:15 bar.
NO_TRADE_1M = bars(
    NO_TRADE_OPEN,
    CandleInterval.M1,
    [
        ("09:15", "1005.00", "1007.00", "1004.00", "1006.00"),
        ("09:16", "1006.00", "1010.00", "1005.00", "1009.00"),
        ("09:17", "1009.00", "1009.00", "1003.00", "1004.00"),
        ("09:18", "1004.00", "1005.00", "1000.00", "1002.00"),
        ("09:19", "1002.00", "1006.00", "1002.00", "1005.00"),
    ],
)

# Hand-calculated expectations (see the module docstring).
SIGNAL_BAR = datetime(2026, 8, 21, 4, 5, tzinfo=UTC)  # 09:35 IST
ENTRY_AT = datetime(2026, 8, 21, 4, 10, tzinfo=UTC)  # 09:40 IST
EXIT_AT = datetime(2026, 8, 21, 4, 25, tzinfo=UTC)  # 09:55 IST
QUANTITY = 100
STOP = Decimal("990.00")
ENTRY_PRICE = Decimal("1000.00")
EXIT_PRICE = Decimal("1019.95")
ENTRY_COSTS = Decimal("30.34")
EXIT_COSTS = Decimal("52.91")
GROSS = Decimal("1995.00")
COSTS = Decimal("83.25")
NET = Decimal("1911.75")
ENDING_EQUITY = Decimal("501911.75")


def synthetic_input(
    candles_5m: tuple[Candle, ...], candles_1m: tuple[Candle, ...]
) -> BacktestInput:
    return make_input(
        candles_5m=candles_5m, candles_1m=candles_1m, cost_schedule=NSE_INTRADAY_EQUITY
    )


def run(backtest_input: BacktestInput, **overrides: object) -> BacktestResult:
    kwargs: dict[str, object] = {
        "starting_capital": CAPITAL,
        "generated_at": GENERATED_AT,
    }
    kwargs.update(overrides)
    return run_backtest(backtest_input, **kwargs)  # type: ignore[arg-type]


#: The trade session needs the no-trade day before it as ATR history.
TRADE_INPUT = synthetic_input(NO_TRADE_5M + TRADE_5M, NO_TRADE_1M + TRADE_1M)
NO_TRADE_INPUT = synthetic_input(NO_TRADE_5M, NO_TRADE_1M)


def assert_the_known_trade(result: BacktestResult) -> None:
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction is SignalDirection.LONG
    assert trade.entry.side is OrderSide.BUY
    assert trade.entry.occurred_at == ENTRY_AT
    assert trade.entry.price == ENTRY_PRICE
    assert trade.entry.reference_price == Decimal("999.95")
    assert trade.entry.quantity == QUANTITY
    assert trade.entry.costs == ENTRY_COSTS
    assert trade.exit.side is OrderSide.SELL
    assert trade.exit.occurred_at == EXIT_AT
    assert trade.exit.price == EXIT_PRICE
    assert trade.exit.reference_price == Decimal("1020.00")
    assert trade.exit.quantity == QUANTITY
    assert trade.exit.costs == EXIT_COSTS
    assert trade.exit_reason is FillReason.TARGET
    assert trade.gross_pnl == GROSS
    assert trade.costs == COSTS
    assert trade.net_pnl == NET


class TestKnownProfitableTrade:
    def test_signal(self) -> None:
        result = run(TRADE_INPUT)
        assert len(result.signal_log) == 1
        record = result.signal_log[0]
        assert record.accepted is True
        assert record.decided_by == "strategy"
        assert record.decision_reason == "LONG_ORB_BREAKOUT"
        assert record.signal.direction is SignalDirection.LONG
        assert record.signal.stop_price == STOP
        assert record.signal.target_r_multiple == Decimal("2.0")
        assert record.signal.signal_bar_start == SIGNAL_BAR

    def test_exact_hand_calculated_trade(self) -> None:
        assert_the_known_trade(run(TRADE_INPUT))

    def test_ending_cash_and_equity(self) -> None:
        result = run(TRADE_INPUT)
        assert [(p.at, p.cash, p.equity) for p in result.equity_curve] == [
            (NO_TRADE_OPEN, CAPITAL, CAPITAL),
            (EXIT_AT, ENDING_EQUITY, ENDING_EQUITY),
        ]

    def test_metrics(self) -> None:
        result = run(TRADE_INPUT)
        assert result.performance is not None
        trades = result.performance.trades
        portfolio = result.performance.portfolio
        assert (trades.trade_count, trades.win_count, trades.loss_count) == (1, 1, 0)
        assert trades.win_rate == Decimal("1")
        assert trades.expectancy_inr == NET
        assert trades.largest_win == NET
        assert trades.profit_factor is None  # no losing trade: undefined, not zero
        assert trades.average_holding_time == timedelta(minutes=15)
        assert portfolio.gross_pnl == GROSS
        assert portfolio.total_costs == COSTS
        assert portfolio.net_pnl == NET
        assert portfolio.starting_capital == CAPITAL
        assert portfolio.ending_equity == ENDING_EQUITY
        assert portfolio.total_return == Decimal("0.0038235")
        assert portfolio.max_drawdown == Decimal("0")
        assert portfolio.time_in_market == timedelta(minutes=15)
        assert portfolio.exposure == Decimal("0.01013513513513513513513513514")
        assert portfolio.active_days == 1
        assert result.performance.risk.mean_return == Decimal("0.0038235")
        assert result.performance.risk.sharpe_per_trade is None  # one observation
        assert result.performance.statistics.t_statistic is None
        assert result.performance.ambiguous_exit_count == 0
        assert result.performance.quarantined_sessions == 0

    def test_manifest(self) -> None:
        manifest = run(TRADE_INPUT).manifest
        assert manifest.input_fingerprint == TRADE_INPUT.fingerprint()
        assert (manifest.strategy_name, manifest.strategy_version) == ("orb", "1")
        assert manifest.engine_version == engine.ENGINE_VERSION
        assert manifest.generated_at == GENERATED_AT


class TestNoTradeSession:
    def test_a_breakout_without_prior_history_is_declined_not_traded(self) -> None:
        """The trade session alone has no earlier bars, so no ATR: the 09:35
        breakout is rejected as ATR_UNAVAILABLE rather than traded."""
        result = run(synthetic_input(TRADE_5M, TRADE_1M))
        assert result.signal_log == ()
        assert result.trades == ()

    def test_nothing_signals_and_nothing_trades(self) -> None:
        result = run(NO_TRADE_INPUT)
        assert result.signal_log == ()
        assert result.trades == ()
        assert [(p.at, p.equity) for p in result.equity_curve] == [(NO_TRADE_OPEN, CAPITAL)]
        assert result.performance is not None
        assert result.performance.portfolio.ending_equity == CAPITAL
        assert result.performance.portfolio.net_pnl == Decimal("0")
        assert result.performance.trades.win_rate is None


class TestSignalLogIsSeparateFromTrades:
    def test_a_signal_on_the_last_bar_is_logged_but_never_traded(self) -> None:
        """Cut the session at the 09:35 signal bar: there is no bar to enter on."""
        result = run(synthetic_input(NO_TRADE_5M + TRADE_5M[:5], NO_TRADE_1M + TRADE_1M))
        assert [r.signal.signal_bar_start for r in result.signal_log] == [SIGNAL_BAR]
        assert result.trades == ()
        assert result.equity_curve[-1].equity == CAPITAL


class RecordingStrategy:
    """Wraps the real ORB strategy and records exactly what it was shown."""

    name = "orb"
    version = "1"

    def __init__(self, events: list[tuple[datetime, int]]) -> None:
        self.inner = OrbStrategy()
        self.events = events
        self.prefixes: list[tuple[Candle, ...]] = []
        self.atrs: list[Decimal | None] = []

    def on_bar(self, session_bars, context: StrategyContext):  # noqa: ANN001, ANN201
        self.prefixes.append(tuple(session_bars))
        self.atrs.append(context.prior_atr)
        self.events.append((session_bars[-1].start_at, 2))
        return self.inner.on_bar(session_bars, context)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[datetime, int]]:
    """Every strategy and execution call as (bar start, step), step 0/1/2 =
    enter/exit/decide. Execution calls also assert they see only later bars."""
    recorded: list[tuple[datetime, int]] = []

    def spy(name: str, step: int) -> None:
        original = getattr(engine, name)

        def wrapper(intent, *args, **kwargs):  # noqa: ANN001, ANN202
            bar = args[-1]  # the one bar this call may read
            assert bar.start_at > intent.signal.signal_bar_start
            for minute in kwargs.get("minute_bars", ()):
                assert bar.start_at <= minute.start_at < bar.end_at
            recorded.append((bar.start_at, step))
            return original(intent, *args, **kwargs)

        monkeypatch.setattr(engine, name, wrapper)

    spy("resolve_entry_fill", 0)
    spy("resolve_exit_fill", 1)
    spy("resolve_hard_exit_fill", 1)
    return recorded


class TestSequencing:
    def test_bars_are_processed_in_time_order_enter_then_exit_then_decide(
        self, events: list[tuple[datetime, int]]
    ) -> None:
        strategy = RecordingStrategy(events)
        assert_the_known_trade(run(TRADE_INPUT, strategy=strategy))

        assert [at for at, step in events if step == 2] == [
            bar.start_at for bar in NO_TRADE_5M + TRADE_5M
        ]
        assert events == sorted(events)
        assert (ENTRY_AT, 0) in events
        assert (datetime(2026, 8, 21, 4, 20, tzinfo=UTC), 1) in events  # 09:50 exit bar

    def test_strategy_sees_only_the_completed_session_prefix(
        self, events: list[tuple[datetime, int]]
    ) -> None:
        strategy = RecordingStrategy(events)
        run(TRADE_INPUT, strategy=strategy)
        expected = [NO_TRADE_5M[: i + 1] for i in range(len(NO_TRADE_5M))] + [
            TRADE_5M[: i + 1] for i in range(len(TRADE_5M))
        ]
        assert strategy.prefixes == expected

    def test_strategy_is_given_the_input_derived_prior_atr(
        self, events: list[tuple[datetime, int]]
    ) -> None:
        strategy = RecordingStrategy(events)
        run(TRADE_INPUT, strategy=strategy)
        assert strategy.atrs == [None] * len(NO_TRADE_5M) + [Decimal("10")] * len(TRADE_5M)


class TestDeterminism:
    def test_the_same_input_twice_serializes_identically(self) -> None:
        first, second = run(TRADE_INPUT), run(TRADE_INPUT)
        serialized = json.dumps(asdict(first), default=str, sort_keys=True)
        assert serialized == json.dumps(asdict(second), default=str, sort_keys=True)
        assert first.canonical() == second.canonical()
        assert first == second


class TestMultiSessionAggregation:
    def test_a_no_trade_day_then_a_trade_day(self) -> None:
        result = run(TRADE_INPUT)
        assert [r.signal.signal_bar_start for r in result.signal_log] == [SIGNAL_BAR]
        assert_the_known_trade(result)
        assert [(p.at, p.equity) for p in result.equity_curve] == [
            (NO_TRADE_OPEN, CAPITAL),
            (EXIT_AT, ENDING_EQUITY),
        ]
        assert result.performance is not None
        portfolio = result.performance.portfolio
        assert portfolio.net_pnl == NET
        assert portfolio.ending_equity == ENDING_EQUITY
        assert portfolio.total_return == Decimal("0.0038235")
        assert portfolio.active_days == 1
        assert result.manifest.input_fingerprint == TRADE_INPUT.fingerprint()

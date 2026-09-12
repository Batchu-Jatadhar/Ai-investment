"""Phase 2.6 final gate: one golden synthetic run through the whole pipeline.

BacktestInput -> prior ATR -> ORB signal -> exact next-bar entry -> slippage ->
exit -> costs -> Trade -> Portfolio -> equity -> metrics -> BacktestResult.

Every expected value below was calculated by hand and is written as a constant.
RELIANCE, tick 0.05, lot 1, NSE_INTRADAY_EQUITY, 1 tick adverse slippage, 1
tick target-through, OrbParams defaults (15m range, 2R, 100,000 notional),
starting capital 500,000. Four IST sessions:

.. rubric:: Wed 2026-08-19 - ATR history

15 bars 09:15-10:25, each 1000.00 / 1005.00 / 995.00 / 1000.00. Every true
range is 10.00; every close sits inside the 995-1005 opening range, and there is
no earlier session, so there is no ATR and no signal.

.. rubric:: Thu 2026-08-20 - no-trade session, ATR available

6 bars 09:15-09:40, the same shape. Prior ATR = Wilder(14) of 14 true ranges of
10.00 = 10.00. Closes never leave 995-1005: no breakout, no signal.

.. rubric:: Fri 2026-08-21 - profitable ORB session

    bar    open     high     low      close
    09:15  1000.00  1004.00   996.00  1002.00  range
    09:20  1002.00  1006.00  1000.00  1001.00  range
    09:25  1001.00  1003.00   994.00   997.00  range
    09:30   997.00  1005.00   996.00  1004.00  1004 <= 1006: no breakout
    09:35  1004.00  1009.00  1003.00  1008.00  1008 > 1006: LONG
    09:40  1007.95  1015.00  1005.00  1012.00  entry bar
    09:45  1012.00  1037.00  1010.00  1030.00  target traded through
    09:50  1030.00  1032.00  1026.00  1028.00  flat; long already signalled

    prior ATR    20 true ranges from Wed + Thu (the overnight one included,
                 prior close 1000 inside 995-1005), all 10.00       = 10.00
    range        high 1006.00, low 994.00, width 12.00 <= 1.5 x 10 = 15
    signal       LONG @ 09:35 bar, stop 994.00, target 2R
    entry        09:40 open 1007.95 + 0.05                          = 1008.00
    quantity     floor(100,000 / 1008.00) = floor(99.206...)        = 99
    risk         1008.00 - 994.00                                   = 14.00
    target       1008.00 + 2 x 14.00 = 1036.00, trigger 1036.05
    exit         09:45 high 1037.00 >= 1036.05, low 1010 > 994
                 1036.00 - 0.05 = 1035.95, stamped at the bar close   09:50
    entry costs  turnover 99 x 1008.00 = 99,792.00
                 brokerage 29.9376 capped 20.00, stamp 2.99376 -> 2.99,
                 exchange 3.0636144 -> 3.06, SEBI 0.099792 -> 0.10,
                 GST 18% x 23.16 = 4.1688 -> 4.17                   = 30.32
    exit costs   turnover 99 x 1035.95 = 102,559.05
                 brokerage 30.767715 capped 20.00, STT 25.6397625 -> 25.64,
                 exchange 3.148562835 -> 3.15, SEBI 0.10255905 -> 0.10,
                 GST 18% x 23.25 = 4.185 -> 4.19 (half up)          = 53.08
    gross        (1035.95 - 1008.00) x 99 = 27.95 x 99              = 2,767.05
    costs        30.32 + 53.08                                      = 83.40
    net          2767.05 - 83.40                                    = 2,683.65
    ending       500,000.00 + 2,683.65                              = 502,683.65
    return       2683.65 / 500000                                   = 0.0053673
    holding      09:40 -> 09:50                                     = 10 min

.. rubric:: Mon 2026-08-24 - execution edge case: the next bucket is missing

    09:15  1030.00  1034.00  1026.00  1031.00  range
    09:20  1031.00  1035.00  1029.00  1030.00  range
    09:25  1030.00  1033.00  1025.00  1028.00  range 1025-1035, width 10.00
    09:30  1028.00  1034.00  1027.00  1033.00  inside
    09:35  1033.00  1040.00  1032.00  1038.00  1038 > 1035: LONG, stop 1025.00
    09:40  -- missing --
    09:45  1038.00  1050.00  1037.00  1048.00  would have won; must not be used
    09:50  1048.00  1052.00  1046.00  1050.00

Monday's ATR folds Friday's true ranges (8, 6, 9, 9, 6, 10, 27, 6) into 10.00
and lands near 10.24, far above the 6.67 the 10.00 range needs. The signal is
accepted, its entry bucket does not exist, and it is recorded NO_EXECUTION_BAR.

.. rubric:: Totals

    signals 2, accepted 2, executed 1, NO_EXECUTION_BAR 1, trades 1
"""

from __future__ import annotations

import inspect
import json
import types
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest import engine
from app.domain.backtest.config import NSE_INTRADAY_EQUITY, SlippageConfig
from app.domain.backtest.engine import run_backtest
from app.domain.backtest.input import BacktestInput
from app.domain.backtest.models import ExecutionStatus, FillReason, OrderSide
from app.domain.backtest.result import BacktestResult
from app.domain.market.models import Candle, CandleInterval
from app.domain.strategy.contract import SignalDirection, StrategyContext
from app.domain.strategy.orb import OrbStrategy
from app.domain.strategy.params import OrbParams
from tests.backtest.conftest import make_candle, make_input

M1, M5 = CandleInterval.M1, CandleInterval.M5
CAPITAL = Decimal("500000")
GENERATED_AT = datetime(2026, 9, 1, tzinfo=UTC)

WED = datetime(2026, 8, 19, 3, 45, tzinfo=UTC)  # 09:15 IST
THU = datetime(2026, 8, 20, 3, 45, tzinfo=UTC)
FRI = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
MON = datetime(2026, 8, 24, 3, 45, tzinfo=UTC)


def at(session: datetime, hh_mm: str) -> datetime:
    hours, minutes = (int(part) for part in hh_mm.split(":"))
    return session + timedelta(hours=hours - 9, minutes=minutes - 15)


def session_bars(
    session: datetime, rows: list[tuple[str, str, str, str, str]]
) -> tuple[Candle, ...]:
    return tuple(
        make_candle(at(session, t), M5, open_=o, high=h, low=lo, close=c) for t, o, h, lo, c in rows
    )


def flat_day(session: datetime, count: int) -> tuple[Candle, ...]:
    return tuple(
        make_candle(
            session + M5.delta * i,
            M5,
            open_="1000.00",
            high="1005.00",
            low="995.00",
            close="1000.00",
        )
        for i in range(count)
    )


WED_5M = flat_day(WED, 15)
THU_5M = flat_day(THU, 6)
FRI_5M = session_bars(
    FRI,
    [
        ("09:15", "1000.00", "1004.00", "996.00", "1002.00"),
        ("09:20", "1002.00", "1006.00", "1000.00", "1001.00"),
        ("09:25", "1001.00", "1003.00", "994.00", "997.00"),
        ("09:30", "997.00", "1005.00", "996.00", "1004.00"),
        ("09:35", "1004.00", "1009.00", "1003.00", "1008.00"),
        ("09:40", "1007.95", "1015.00", "1005.00", "1012.00"),
        ("09:45", "1012.00", "1037.00", "1010.00", "1030.00"),
        ("09:50", "1030.00", "1032.00", "1026.00", "1028.00"),
    ],
)
MON_5M = session_bars(
    MON,
    [
        ("09:15", "1030.00", "1034.00", "1026.00", "1031.00"),
        ("09:20", "1031.00", "1035.00", "1029.00", "1030.00"),
        ("09:25", "1030.00", "1033.00", "1025.00", "1028.00"),
        ("09:30", "1028.00", "1034.00", "1027.00", "1033.00"),
        ("09:35", "1033.00", "1040.00", "1032.00", "1038.00"),
        ("09:45", "1038.00", "1050.00", "1037.00", "1048.00"),
        ("09:50", "1048.00", "1052.00", "1046.00", "1050.00"),
    ],
)
#: One minute per session - the input requires 1m coverage, and no exit here is
#: ambiguous, so no minute ever decides anything.
MINUTES = tuple(make_candle(day, M1) for day in (WED, THU, FRI, MON))


def golden_input(**overrides: object) -> BacktestInput:
    values: dict[str, object] = {
        "candles_5m": WED_5M + THU_5M + FRI_5M + MON_5M,
        "candles_1m": MINUTES,
        "cost_schedule": NSE_INTRADAY_EQUITY,
    }
    values.update(overrides)
    return make_input(**values)  # type: ignore[arg-type]


def run_golden(
    generated_at: datetime = GENERATED_AT, runner=run_backtest, **kwargs: object
) -> BacktestResult:  # noqa: ANN001
    return runner(golden_input(), starting_capital=CAPITAL, generated_at=generated_at, **kwargs)


# --- hand-calculated expectations -------------------------------------------
ENGINE_VERSION = "2.6.3"
FRI_SIGNAL_AT = at(FRI, "09:35")
MON_SIGNAL_AT = at(MON, "09:35")
STOP = Decimal("994.00")
TARGET_R = Decimal("2.0")
TARGET_LEVEL = Decimal("1036.00")
ENTRY_AT = at(FRI, "09:40")
ENTRY_REFERENCE = Decimal("1007.95")
ENTRY_PRICE = Decimal("1008.00")
QUANTITY = 99
EXIT_AT = at(FRI, "09:50")
EXIT_PRICE = Decimal("1035.95")
ENTRY_COSTS = Decimal("30.32")
EXIT_COSTS = Decimal("53.08")
GROSS = Decimal("2767.05")
COSTS = Decimal("83.40")
NET = Decimal("2683.65")
ENDING = Decimal("502683.65")
TOTAL_RETURN = Decimal("0.0053673")


def assert_golden(result: BacktestResult) -> None:
    """Every exact figure of the golden run."""
    summary = result.canonical()
    assert (
        summary["signal_count"],
        summary["accepted_signal_count"],
        summary["executed_signal_count"],
        summary["no_execution_bar_signal_count"],
        summary["trade_count"],
    ) == ("2", "2", "1", "1", "1")

    fri, mon = result.signal_log
    assert (fri.signal.direction, fri.signal.signal_bar_start) == (
        SignalDirection.LONG,
        FRI_SIGNAL_AT,
    )
    assert (fri.signal.stop_price, fri.signal.target_r_multiple) == (STOP, TARGET_R)
    assert (fri.accepted, fri.decision_reason, fri.execution_status) == (
        True,
        "LONG_ORB_BREAKOUT",
        ExecutionStatus.FILLED,
    )
    assert (mon.signal.direction, mon.signal.signal_bar_start, mon.signal.stop_price) == (
        SignalDirection.LONG,
        MON_SIGNAL_AT,
        Decimal("1025.00"),
    )
    assert (mon.accepted, mon.execution_status) == (True, ExecutionStatus.NO_EXECUTION_BAR)

    (trade,) = result.trades
    assert trade.direction is SignalDirection.LONG
    assert (trade.entry.side, trade.entry.occurred_at, trade.entry.bar_start) == (
        OrderSide.BUY,
        ENTRY_AT,
        ENTRY_AT,
    )
    assert (trade.entry.reference_price, trade.entry.price) == (ENTRY_REFERENCE, ENTRY_PRICE)
    assert (trade.entry.quantity, trade.exit.quantity) == (QUANTITY, QUANTITY)
    assert (trade.exit.side, trade.exit.occurred_at, trade.exit_reason) == (
        OrderSide.SELL,
        EXIT_AT,
        FillReason.TARGET,
    )
    assert (trade.exit.reference_price, trade.exit.price) == (TARGET_LEVEL, EXIT_PRICE)
    assert (trade.entry.costs, trade.exit.costs) == (ENTRY_COSTS, EXIT_COSTS)
    assert (trade.gross_pnl, trade.costs, trade.net_pnl) == (GROSS, COSTS, NET)

    assert [(p.at, p.cash, p.equity) for p in result.equity_curve] == [
        (WED, CAPITAL, CAPITAL),
        (EXIT_AT, ENDING, ENDING),
    ]

    assert result.performance is not None
    trades, portfolio = result.performance.trades, result.performance.portfolio
    assert (trades.trade_count, trades.win_count, trades.loss_count) == (1, 1, 0)
    assert (trades.win_rate, trades.expectancy_inr, trades.largest_win) == (Decimal(1), NET, NET)
    assert trades.profit_factor is None
    assert (trades.long_count, trades.long_net_pnl, trades.short_count) == (1, NET, 0)
    assert trades.average_holding_time == timedelta(minutes=10)
    assert (portfolio.gross_pnl, portfolio.total_costs, portfolio.net_pnl) == (GROSS, COSTS, NET)
    assert (portfolio.starting_capital, portfolio.ending_equity) == (CAPITAL, ENDING)
    assert portfolio.total_return == TOTAL_RETURN
    assert (portfolio.max_drawdown, portfolio.max_drawdown_pct) == (Decimal(0), None)
    assert portfolio.time_in_market == timedelta(minutes=10)
    assert portfolio.active_days == 1
    assert result.performance.risk.mean_return == TOTAL_RETURN
    assert result.performance.risk.sharpe_per_trade is None
    assert result.performance.statistics.t_statistic is None
    assert (
        result.performance.ambiguous_exit_count,
        result.performance.pessimistic_fallback_count,
        result.performance.quarantined_sessions,
    ) == (0, 0, 0)

    assert result.manifest.input_fingerprint == golden_input().fingerprint()
    assert result.manifest.engine_version == ENGINE_VERSION
    assert (result.manifest.strategy_name, result.manifest.strategy_version) == ("orb", "1")
    assert result.manifest.generated_at == GENERATED_AT
    assert summary["engine_version"] == ENGINE_VERSION
    assert summary["net_pnl"] == "2683.65"


def test_golden_scenario() -> None:
    backtest_input = golden_input()
    assert backtest_input.prior_atr(WED.date()) is None
    assert backtest_input.prior_atr(THU.date()) == Decimal("10")
    assert backtest_input.prior_atr(FRI.date()) == Decimal("10")
    assert_golden(run_golden())


class Recording:
    """The real ORB strategy, recording every signal it generated."""

    name, version = "orb", "1"

    def __init__(self) -> None:
        self.inner = OrbStrategy()
        self.generated: list[object] = []

    def on_bar(self, session_bars, context: StrategyContext):  # noqa: ANN001, ANN201
        signal = self.inner.on_bar(session_bars, context)
        if signal is not None:
            self.generated.append(signal)
        return signal


def test_result_is_complete_and_sessions_are_isolated() -> None:
    strategy = Recording()
    result = run_golden(strategy=strategy)

    # Every generated signal is logged, in order, and nothing else is.
    assert [record.signal for record in result.signal_log] == strategy.generated

    # Each trade traces to exactly one accepted, FILLED signal whose next bucket
    # it entered on, in the same IST session; no other signal traded.
    filled = [r for r in result.signal_log if r.execution_status is ExecutionStatus.FILLED]
    assert len(filled) == len(result.trades)
    for record, trade in zip(filled, result.trades, strict=True):
        assert record.accepted
        assert trade.direction is record.signal.direction
        assert trade.entry.bar_start == record.signal.signal_bar_start + M5.delta
        assert trade.entry.bar_start.date() == record.signal.signal_bar_start.date()

    # Monday's unexecutable signal stays Monday's: nothing entered on its later
    # bars, and nothing from Friday reached Monday.
    assert all(t.entry.bar_start < MON for t in result.trades)


def test_manifest_fingerprint_identifies_every_run_assumption() -> None:
    """The manifest carries the input fingerprint; the fingerprint's canonical
    payload is what it is taken over, and it names every assumption."""
    backtest_input = golden_input()
    manifest = run_golden().manifest
    assert manifest.input_fingerprint == backtest_input.fingerprint()
    assert manifest.engine_version == ENGINE_VERSION

    payload = backtest_input.canonical_payload()
    assert payload["strategy_params"] == OrbParams().canonical()
    assert payload["cost_schedule"]["schedule_id"] == "nse-intraday-equity"  # type: ignore[index]
    assert payload["cost_schedule"]["version"] == "2026-03-01"  # type: ignore[index]
    assert payload["slippage_config"] == {"adverse_ticks": "1", "model_id": "fixed_ticks"}
    assert payload["execution_config"] == backtest_input.execution_config.canonical()
    assert payload["prior_atr"] == {
        "method": "wilder",
        "period": "14",
        "source": "completed signal bars strictly before the session",
    }
    signal_series = payload["candles_5m"]
    assert (signal_series["count"], signal_series["first"], signal_series["last"]) == (  # type: ignore[index]
        str(15 + 6 + 8 + 7),
        "2026-08-19T03:45:00+00:00",
        "2026-08-24T04:20:00+00:00",
    )
    assert payload["instrument"]["instrument_token"] == "738561"  # type: ignore[index]


def test_exposing_one_future_bar_to_the_strategy_breaks_the_golden_run() -> None:
    """Mutation check: rebuild the engine with the strategy handed one bar past
    the one being decided, and the golden assertions must fail."""
    source = inspect.getsource(engine)
    assert source.count("bars[: index + 1]") == 1
    mutated = types.ModuleType("mutated_engine")
    exec(  # noqa: S102
        compile(source.replace("bars[: index + 1]", "bars[: index + 2]"), "mutated", "exec"),
        mutated.__dict__,
    )

    assert_golden(run_golden())  # the real engine passes
    leaked = run_golden(runner=mutated.run_backtest)  # runs to completion
    with pytest.raises(AssertionError):
        assert_golden(leaked)
    # Seeing 09:35 while deciding 09:30, Friday's long fires a bar early and its
    # entry bucket no longer lines up, so the profitable trade never happens.
    assert leaked.trades == ()


def artifact(result: BacktestResult) -> bytes:
    """The whole result, serialized, minus the one wall-clock field."""
    rendered = asdict(result)
    del rendered["manifest"]["generated_at"]
    return json.dumps(rendered, default=str, sort_keys=True, separators=(",", ":")).encode()


def test_repeat_runs_produce_a_byte_identical_result_artifact() -> None:
    first = run_golden(generated_at=datetime(2026, 9, 1, tzinfo=UTC))
    second = run_golden(generated_at=datetime(2031, 1, 1, 12, 34, tzinfo=UTC))

    assert artifact(first) == artifact(second)
    assert first.canonical() == second.canonical()
    # generated_at is the only difference between the two runs.
    assert first != second
    assert replace(second, manifest=first.manifest) == first


def test_the_fingerprint_tracks_data_strategy_and_config() -> None:
    baseline = golden_input().fingerprint()
    assert golden_input().fingerprint() == baseline

    moved = FRI_5M[6]  # the 09:45 bar
    data_changed = WED_5M + THU_5M + FRI_5M[:6] + (replace(moved, high=Decimal("1037.05")),)
    assert golden_input(candles_5m=data_changed + FRI_5M[7:] + MON_5M).fingerprint() != baseline
    assert (
        golden_input(strategy_params=OrbParams(target_r_multiple=Decimal("3.0"))).fingerprint()
        != baseline
    )
    assert golden_input(slippage_config=SlippageConfig(adverse_ticks=2)).fingerprint() != baseline

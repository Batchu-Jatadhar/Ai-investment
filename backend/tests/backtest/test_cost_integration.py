"""Costs from the fill all the way to the metrics, hand-calculated.

The cost model has its own unit tests; this checks the wiring - that a charge
computed per leg survives into the Trade, the cash balance, the equity curve and
the report, with gross and net still separately visible at every step.

.. rubric:: The worked trade

71 shares under NSE_INTRADAY_EQUITY, one tick of adverse slippage:

    entry   bar opens 1399.95, buy fills 1400.00
            turnover 71 x 1400.00                      =  99,400.00
            brokerage 20.00 (0.03% = 29.82, capped)
            exchange 3.05, SEBI 0.10, stamp duty 2.98
            GST 18% of (20.00 + 0.10 + 3.05) = 4.17
            no STT - buy leg                     costs  =      30.30

    exit    2R target: entry 1400.00, stop 1390.00, so risk 10.00
            and the target is 1420.00; sell fills 1419.95
            turnover 71 x 1419.95                      = 100,816.45
            brokerage 20.00 (0.03% = 30.24, capped)
            STT 25.20, exchange 3.10, SEBI 0.10
            GST 18% of (20.00 + 0.10 + 3.10) = 4.18
            no stamp duty - sell leg             costs  =      52.58

    gross   (1419.95 - 1400.00) x 71                    =   1,416.45
    costs   30.30 + 52.58                               =      82.88
    net     1416.45 - 82.88                             =   1,333.57
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.backtest.cash import build_equity_curve
from app.domain.backtest.config import (
    NO_COSTS,
    NSE_INTRADAY_EQUITY,
    ExecutionConfig,
    SlippageConfig,
)
from app.domain.backtest.costs import leg_charges
from app.domain.backtest.execution import (
    ExecutionIntent,
    resolve_entry_fill,
    resolve_stop_fill,
    resolve_target_fill,
)
from app.domain.backtest.metrics import evaluate_performance
from app.domain.backtest.models import Fill, OrderSide, Trade
from app.domain.backtest.portfolio import Portfolio
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.strategy.contract import Signal, SignalDirection

SCHEDULE = NSE_INTRADAY_EQUITY
TICK = Decimal("0.05")
SLIPPAGE = SlippageConfig()
QUANTITY = 71
CAPITAL = Decimal("500000")

START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
ENTRY_BAR = START + timedelta(minutes=5)
EXIT_BAR = ENTRY_BAR + CandleInterval.M5.delta

ENTRY_TURNOVER = Decimal("99400.00")
TARGET_TURNOVER = Decimal("100816.45")
STOP_TURNOVER = Decimal("98686.45")


def intent() -> ExecutionIntent:
    signal = Signal(
        instrument_token=738561,
        direction=SignalDirection.LONG,
        stop_price=Decimal("1390.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=START,
        reason="LONG_ORB_BREAKOUT",
    )
    return ExecutionIntent(signal=signal, quantity=QUANTITY, entry_bar_start=ENTRY_BAR)


def bar(at: datetime, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(
        instrument_token=738561,
        interval=CandleInterval.M5,
        start_at=at,
        end_at=at + CandleInterval.M5.delta,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=5000,
        status=CandleStatus.COMPLETED,
        source="test",
    )


def entry_fill(schedule=SCHEDULE) -> Fill:  # noqa: ANN001
    outcome = resolve_entry_fill(
        intent(),
        bar(ENTRY_BAR, "1399.95", "1405", "1398", "1402"),
        tick_size=TICK,
        slippage=SLIPPAGE,
        cost_schedule=schedule,
    )
    assert outcome.fill is not None
    return outcome.fill


def winning_trade(schedule=SCHEDULE) -> Trade:  # noqa: ANN001
    entry = entry_fill(schedule)
    exit_fill = resolve_target_fill(
        intent(),
        entry,
        bar(EXIT_BAR, "1405", "1425", "1404", "1422"),
        tick_size=TICK,
        execution=ExecutionConfig(),
        slippage=SLIPPAGE,
        cost_schedule=schedule,
    )
    assert exit_fill is not None
    from app.domain.backtest.position import PositionBook

    _, trade = PositionBook().enter(intent(), entry).close(exit_fill)
    return trade


def losing_trade(schedule=SCHEDULE) -> Trade:  # noqa: ANN001
    entry = entry_fill(schedule)
    exit_fill = resolve_stop_fill(
        intent(),
        bar(EXIT_BAR, "1398", "1400", "1385", "1388"),
        tick_size=TICK,
        slippage=SLIPPAGE,
        cost_schedule=schedule,
    )
    assert exit_fill is not None
    from app.domain.backtest.position import PositionBook

    _, trade = PositionBook().enter(intent(), entry).close(exit_fill)
    return trade


class TestFillsAreBornCharged:
    def test_the_entry_leg(self) -> None:
        fill = entry_fill()

        assert fill.price == Decimal("1400.00")
        assert fill.price * Decimal(QUANTITY) == ENTRY_TURNOVER
        assert fill.costs == Decimal("30.30")

    def test_the_exit_leg(self) -> None:
        trade = winning_trade()

        assert trade.exit.price == Decimal("1419.95")
        assert trade.exit.price * Decimal(QUANTITY) == TARGET_TURNOVER
        assert trade.exit.costs == Decimal("52.58")

    def test_the_charge_is_taken_on_the_value_actually_transacted(self) -> None:
        """Slippage is part of what was paid. Charging the reference level would
        understate the bill on every fill that slipped."""
        fill = entry_fill()

        assert fill.reference_price == Decimal("1399.95")
        assert fill.price == Decimal("1400.00")
        assert (
            fill.costs
            == leg_charges(
                SCHEDULE, side=OrderSide.BUY, turnover=fill.price * Decimal(QUANTITY)
            ).total
        )

    def test_every_component_is_accounted_for(self) -> None:
        """The fill carries the total; the breakdown is reproducible from the
        side, the turnover and the schedule, all of which the fill records."""
        charges = leg_charges(SCHEDULE, side=OrderSide.BUY, turnover=ENTRY_TURNOVER)

        assert charges.brokerage == Decimal("20.00")
        assert charges.stt == Decimal("0.00")
        assert charges.exchange_transaction == Decimal("3.05")
        assert charges.sebi_turnover == Decimal("0.10")
        assert charges.stamp_duty == Decimal("2.98")
        assert charges.gst == Decimal("4.17")
        assert charges.total == entry_fill().costs == Decimal("30.30")


class TestBuySellAsymmetryIsPreserved:
    def test_the_buy_pays_stamp_duty_and_no_stt(self) -> None:
        charges = leg_charges(SCHEDULE, side=OrderSide.BUY, turnover=ENTRY_TURNOVER)
        assert charges.stamp_duty > 0
        assert charges.stt == Decimal("0.00")

    def test_the_sell_pays_stt_and_no_stamp_duty(self) -> None:
        charges = leg_charges(SCHEDULE, side=OrderSide.SELL, turnover=TARGET_TURNOVER)
        assert charges.stt == Decimal("25.20")
        assert charges.stamp_duty == Decimal("0.00")

    def test_the_two_legs_are_charged_differently(self) -> None:
        """Not one blended percentage: the same turnover costs a different
        amount depending on which way it went."""
        same = Decimal("100000.00")
        buy = leg_charges(SCHEDULE, side=OrderSide.BUY, turnover=same)
        sell = leg_charges(SCHEDULE, side=OrderSide.SELL, turnover=same)

        assert buy.total != sell.total


class TestTradeAccounting:
    def test_the_winning_trade_to_the_paisa(self) -> None:
        trade = winning_trade()

        assert trade.gross_pnl == Decimal("1416.45")
        assert trade.costs == Decimal("82.88")
        assert trade.net_pnl == Decimal("1333.57")

    def test_the_losing_trade_to_the_paisa(self) -> None:
        """Stopped at 1390.00 less a tick: 71 x -10.05 gross, and the costs make
        it worse rather than better."""
        trade = losing_trade()

        assert trade.exit.price == Decimal("1389.95")
        assert trade.exit.price * Decimal(QUANTITY) == STOP_TURNOVER
        assert trade.gross_pnl == Decimal("-713.55")
        assert trade.costs == Decimal("82.26")
        assert trade.net_pnl == Decimal("-795.81")

    def test_gross_less_costs_is_net_either_way(self) -> None:
        for trade in (winning_trade(), losing_trade()):
            assert trade.net_pnl == trade.gross_pnl - trade.costs
            assert trade.costs == trade.entry.costs + trade.exit.costs

    def test_gross_and_net_stay_separately_visible(self) -> None:
        """Nothing collapses the two. A run that cannot show what it paid
        cannot be checked against a contract note."""
        trade = winning_trade()
        assert trade.gross_pnl != trade.net_pnl
        assert trade.costs > 0


class TestCostsReachTheAccounts:
    def test_cash_and_equity_carry_the_charges(self) -> None:
        """500,000 + 1333.57 net, not + 1416.45 gross."""
        trade = winning_trade()
        portfolio = (
            Portfolio.funded(CAPITAL, started_at=START, fixed_notional=Decimal("100000"))
            .enter(intent(), trade.entry)
            .close(trade.exit)
        )

        assert portfolio.realized_net_pnl == Decimal("1333.57")
        assert portfolio.cash == CAPITAL + Decimal("1333.57")
        assert portfolio.equity_curve[-1].equity == Decimal("501333.57")

    def test_metrics_report_gross_costs_and_net_separately(self) -> None:
        trades = (winning_trade(),)
        curve = build_equity_curve(trades, starting_capital=CAPITAL, start_at=START)
        report = evaluate_performance(trades, curve)

        assert report.portfolio.gross_pnl == Decimal("1416.45")
        assert report.portfolio.total_costs == Decimal("82.88")
        assert report.portfolio.net_pnl == Decimal("1333.57")
        assert report.portfolio.net_pnl == (
            report.portfolio.gross_pnl - report.portfolio.total_costs
        )
        assert report.trades.expectancy_inr == Decimal("1333.57")


class TestTheFreeScheduleStillWorks:
    def test_no_costs_charges_nothing(self) -> None:
        """The Phase 2.3/2.4 behaviour, kept as a named value so a free run is
        visible as a choice rather than an omission."""
        trade = winning_trade(NO_COSTS)

        assert trade.costs == Decimal("0.00")
        assert trade.net_pnl == trade.gross_pnl == Decimal("1416.45")
        assert NO_COSTS.rates_verified is False

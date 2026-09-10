"""Realized cash accounting, with every rupee traced.

Trades are built through PositionBook rather than by hand, so the ledger is
tested against the same Trade objects the engine will hand it.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.cash import CashLedger
from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import Fill, FillReason, OrderSide, Trade
from app.domain.backtest.position import PositionBook
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN

CAPITAL = Decimal("500000")
ENTRY_BAR = SESSION_OPEN + timedelta(minutes=5)
EXIT_BAR = SESSION_OPEN + timedelta(minutes=30)
QUANTITY = 70


def intent(direction: SignalDirection) -> ExecutionIntent:
    signal = Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=direction,
        stop_price=Decimal("1390.00") if direction.is_long else Decimal("1412.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=SESSION_OPEN,
        reason="test",
    )
    return ExecutionIntent(signal=signal, quantity=QUANTITY, entry_bar_start=ENTRY_BAR)


def fill(side: OrderSide, reason: FillReason, price: str, costs: str) -> Fill:
    at = ENTRY_BAR if reason is FillReason.ENTRY else EXIT_BAR
    return Fill(
        side=side,
        reason=reason,
        quantity=QUANTITY,
        price=Decimal(price),
        reference_price=Decimal(price),
        slippage_per_unit=Decimal("0"),
        costs=Decimal(costs),
        occurred_at=at,
        bar_start=at,
    )


def round_trip(
    direction: SignalDirection,
    entry_price: str,
    exit_price: str,
    *,
    entry_costs: str = "20.00",
    exit_costs: str = "25.00",
    exit_reason: FillReason = FillReason.TARGET,
) -> tuple[Fill, Trade]:
    """One complete trade, built through the position lifecycle."""
    entry_side = OrderSide.BUY if direction.is_long else OrderSide.SELL
    entry = fill(entry_side, FillReason.ENTRY, entry_price, entry_costs)
    exit_fill = fill(entry_side.opposite, exit_reason, exit_price, exit_costs)

    _, trade = PositionBook().enter(intent(direction), entry).close(exit_fill)
    return entry, trade


def settle(direction: SignalDirection, entry_price: str, exit_price: str, **kw: str) -> CashLedger:
    entry, trade = round_trip(direction, entry_price, exit_price, **kw)  # type: ignore[arg-type]
    return CashLedger.funded(CAPITAL).after_entry(entry).after_exit(trade)


class TestLongTrades:
    def test_a_profitable_long(self) -> None:
        """Buy 70 at 1400.00 for 98,020 including costs; sell at 1420.00 for
        99,375 net of costs. 500,000 - 98,020 + 99,375 = 501,355."""
        entry, trade = round_trip(SignalDirection.LONG, "1400.00", "1420.00")

        held = CashLedger.funded(CAPITAL).after_entry(entry)
        assert held.cash == Decimal("401980.00")
        assert held.closed_trades == 0
        assert held.realized_net_pnl == Decimal("0")

        settled = held.after_exit(trade)
        assert settled.cash == Decimal("501355.00")
        assert settled.realized_gross_pnl == Decimal("1400.00")
        assert settled.realized_costs == Decimal("45.00")
        assert settled.realized_net_pnl == Decimal("1355.00")
        assert settled.closed_trades == 1

    def test_a_losing_long(self) -> None:
        """Sold 10.00 lower: -700 gross, -745 after costs."""
        settled = settle(SignalDirection.LONG, "1400.00", "1390.00")

        assert settled.realized_gross_pnl == Decimal("-700.00")
        assert settled.realized_net_pnl == Decimal("-745.00")
        assert settled.cash == Decimal("499255.00")


class TestShortTrades:
    def test_a_profitable_short(self) -> None:
        """The sell comes in first, so cash rises before it falls again."""
        entry, trade = round_trip(SignalDirection.SHORT, "1400.00", "1380.00")

        held = CashLedger.funded(CAPITAL).after_entry(entry)
        assert held.cash == Decimal("597980.00")

        settled = held.after_exit(trade)
        assert settled.realized_gross_pnl == Decimal("1400.00")
        assert settled.realized_net_pnl == Decimal("1355.00")
        assert settled.cash == Decimal("501355.00")

    def test_a_losing_short(self) -> None:
        """Bought back 12.00 higher: -840 gross, -885 after costs."""
        settled = settle(SignalDirection.SHORT, "1400.00", "1412.00", exit_reason=FillReason.STOP)

        assert settled.realized_gross_pnl == Decimal("-840.00")
        assert settled.realized_net_pnl == Decimal("-885.00")
        assert settled.cash == Decimal("499115.00")


class TestCosts:
    def test_costs_reduce_cash_by_exactly_their_total(self) -> None:
        """The same trade, once free and once charged. The whole difference is
        the 45.00 of costs, on both the cash balance and the net."""
        free = settle(SignalDirection.LONG, "1400.00", "1420.00", entry_costs="0", exit_costs="0")
        charged = settle(SignalDirection.LONG, "1400.00", "1420.00")

        assert free.realized_gross_pnl == charged.realized_gross_pnl
        assert free.cash - charged.cash == Decimal("45.00")
        assert free.realized_net_pnl - charged.realized_net_pnl == Decimal("45.00")

    def test_costs_are_charged_on_both_legs(self) -> None:
        settled = settle(SignalDirection.LONG, "1400.00", "1420.00")
        assert settled.realized_costs == Decimal("45.00")

    def test_costs_turn_a_thin_winner_into_a_loser(self) -> None:
        """0.50 a share on 70 is 35.00 gross, less than the 45.00 it cost to
        trade. Gross and net must disagree in sign, or costs are not landing."""
        settled = settle(SignalDirection.LONG, "1400.00", "1400.50")

        assert settled.realized_gross_pnl == Decimal("35.00")
        assert settled.realized_net_pnl == Decimal("-10.00")
        assert settled.cash == Decimal("499990.00")


class TestConservation:
    def test_starting_capital_plus_net_equals_ending_cash(self) -> None:
        """The identity this module exists for, across all four outcomes."""
        cases = (
            (SignalDirection.LONG, "1400.00", "1420.00"),
            (SignalDirection.LONG, "1400.00", "1390.00"),
            (SignalDirection.SHORT, "1400.00", "1380.00"),
            (SignalDirection.SHORT, "1400.00", "1412.00"),
        )
        for direction, entry_price, exit_price in cases:
            settled = settle(direction, entry_price, exit_price)
            assert settled.cash == settled.starting_capital + settled.realized_net_pnl

    def test_the_identity_holds_across_several_trades(self) -> None:
        ledger = CashLedger.funded(CAPITAL)
        for entry_price, exit_price in (
            ("1400.00", "1420.00"),
            ("1420.00", "1405.00"),
            ("1405.00", "1433.00"),
        ):
            entry, trade = round_trip(SignalDirection.LONG, entry_price, exit_price)
            ledger = ledger.after_entry(entry).after_exit(trade)

        assert ledger.closed_trades == 3
        assert ledger.cash == ledger.starting_capital + ledger.realized_net_pnl

    def test_prices_with_paise_stay_exact(self) -> None:
        """1400.05 to 1420.15 is 20.10 a share on 70 - 1407.00 exactly, with no
        residue a float would have left behind."""
        settled = settle(SignalDirection.LONG, "1400.05", "1420.15")

        assert settled.realized_gross_pnl == Decimal("1407.00")
        assert settled.realized_net_pnl == Decimal("1362.00")
        assert settled.cash == Decimal("501362.00")
        assert settled.cash == settled.starting_capital + settled.realized_net_pnl


class TestLedgerShape:
    def test_a_new_ledger_starts_flat(self) -> None:
        ledger = CashLedger.funded(CAPITAL)
        assert ledger.cash == CAPITAL == ledger.starting_capital
        assert ledger.realized_gross_pnl == ledger.realized_costs == Decimal("0")
        assert ledger.closed_trades == 0

    def test_every_movement_returns_a_new_ledger(self) -> None:
        """No hidden state: a ledger handed to two code paths cannot be changed
        underneath either of them."""
        start = CashLedger.funded(CAPITAL)
        entry, _ = round_trip(SignalDirection.LONG, "1400.00", "1420.00")
        moved = start.after_entry(entry)

        assert start.cash == CAPITAL
        assert moved.cash != CAPITAL
        with pytest.raises(dataclasses.FrozenInstanceError):
            start.cash = Decimal("1")  # type: ignore[misc]

    def test_an_exit_fill_cannot_be_applied_as_an_entry(self) -> None:
        """An exit also realizes a trade, so it must go through after_exit or
        the realized totals would silently never move."""
        _, trade = round_trip(SignalDirection.LONG, "1400.00", "1420.00")

        with pytest.raises(ValueError, match="after_entry needs an ENTRY fill"):
            CashLedger.funded(CAPITAL).after_entry(trade.exit)

    def test_float_money_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            CashLedger(starting_capital=500000.0, cash=CAPITAL)  # type: ignore[arg-type]

    def test_a_run_must_state_what_it_began_with(self) -> None:
        with pytest.raises(ValueError, match="starting_capital must be positive"):
            CashLedger.funded(Decimal("0"))

    def test_the_ledger_holds_no_unrealised_value(self) -> None:
        """It knows what was realized, never what an open position might be
        worth - that needs a price which is not part of the ledger."""
        names = {f.name for f in dataclasses.fields(CashLedger)}
        assert names == {
            "starting_capital",
            "cash",
            "realized_gross_pnl",
            "realized_costs",
            "closed_trades",
        }
        assert (
            names
            & {
                "equity",
                "unrealised_pnl",
                "market_value",
                "drawdown",
                "peak",
                "returns",
            }
            == set()
        )

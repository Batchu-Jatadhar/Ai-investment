"""Portfolio integration: the four accounting components moving together.

The pieces each have their own tests. What is checked here is that a whole
trade, driven through the portfolio, leaves the book, the cash, the trade log
and the equity curve all telling the same story.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import Fill, FillReason, OrderSide
from app.domain.backtest.portfolio import Portfolio
from app.domain.backtest.position import PositionTransitionError
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN

CAPITAL = Decimal("500000")
NOTIONAL = Decimal("100000")
ENTRY_BAR = SESSION_OPEN + timedelta(minutes=5)

#: 100,000 / 1400.00 is 71.42..., so the fixed notional buys 71 shares.
SIZE = 71


def portfolio() -> Portfolio:
    return Portfolio.funded(CAPITAL, started_at=SESSION_OPEN, fixed_notional=NOTIONAL)


def intent(direction: SignalDirection, quantity: int = SIZE) -> ExecutionIntent:
    signal = Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=direction,
        stop_price=Decimal("1390.00") if direction.is_long else Decimal("1412.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=SESSION_OPEN,
        reason="test",
    )
    return ExecutionIntent(signal=signal, quantity=quantity, entry_bar_start=ENTRY_BAR)


def leg(
    side: OrderSide,
    reason: FillReason,
    price: str,
    costs: str,
    minutes: int,
    quantity: int = SIZE,
) -> Fill:
    at = SESSION_OPEN + timedelta(minutes=minutes)
    return Fill(
        side=side,
        reason=reason,
        quantity=quantity,
        price=Decimal(price),
        reference_price=Decimal(price),
        slippage_per_unit=Decimal("0"),
        costs=Decimal(costs),
        occurred_at=at,
        bar_start=at,
    )


def entry_leg(direction: SignalDirection, price: str = "1400.00") -> Fill:
    side = OrderSide.BUY if direction.is_long else OrderSide.SELL
    return leg(side, FillReason.ENTRY, price, "20.00", 5)


def exit_leg(direction: SignalDirection, price: str, minutes: int = 30) -> Fill:
    side = OrderSide.SELL if direction.is_long else OrderSide.BUY
    return leg(side, FillReason.TARGET, price, "25.00", minutes)


class TestSizingIsWiredIn:
    def test_the_fixed_notional_decides_the_share_count(self) -> None:
        assert portfolio().size_for(Decimal("1400.00")) == SIZE

    def test_sizing_ignores_the_balance(self) -> None:
        """Phase 2 sizing must not vary with how the run is going, or Phase 2
        and Phase 3 could not be compared."""
        rich = Portfolio.funded(
            Decimal("5000000"), started_at=SESSION_OPEN, fixed_notional=NOTIONAL
        )
        assert rich.size_for(Decimal("1400.00")) == portfolio().size_for(Decimal("1400.00"))


class TestCompleteTrades:
    def test_a_long_round_trip(self) -> None:
        """71 at 1400.00 costs 99,420 with fees; sold at 1420.00 returns
        100,795. Gross 1,420.00, costs 45.00, net 1,375.00."""
        held = portfolio().enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

        assert not held.is_flat
        assert held.cash == Decimal("400580.00")
        assert held.trades == ()

        done = held.close(exit_leg(SignalDirection.LONG, "1420.00"))

        assert done.is_flat
        assert done.cash == Decimal("501375.00")
        assert done.realized_net_pnl == Decimal("1375.00")
        assert len(done.trades) == 1
        assert done.trades[0].gross_pnl == Decimal("1420.00")
        assert done.trades[0].exit_reason is FillReason.TARGET

    def test_a_short_round_trip(self) -> None:
        """The sale comes in first, so cash rises before it falls again."""
        held = portfolio().enter(intent(SignalDirection.SHORT), entry_leg(SignalDirection.SHORT))

        assert held.cash == Decimal("599380.00")

        done = held.close(exit_leg(SignalDirection.SHORT, "1380.00"))

        assert done.is_flat
        assert done.cash == Decimal("501375.00")
        assert done.realized_net_pnl == Decimal("1375.00")

    def test_a_losing_trade_lowers_the_balance(self) -> None:
        done = (
            portfolio()
            .enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))
            .close(exit_leg(SignalDirection.LONG, "1390.00"))
        )

        assert done.realized_net_pnl == Decimal("-755.00")
        assert done.cash == Decimal("499245.00")


class TestSequentialTrades:
    def run_three(self) -> Portfolio:
        book = portfolio()
        for index, (entry_price, exit_price) in enumerate(
            (("1400.00", "1420.00"), ("1420.00", "1410.00"), ("1410.00", "1430.00"))
        ):
            book = book.enter(
                intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG, entry_price)
            ).close(exit_leg(SignalDirection.LONG, exit_price, minutes=30 + index * 60))
        return book

    def test_three_trades_accumulate(self) -> None:
        done = self.run_three()

        assert len(done.trades) == 3
        assert done.ledger.closed_trades == 3
        assert done.is_flat

    def test_the_book_is_reusable_after_each_close(self) -> None:
        """A session of several trades needs the cycle to repeat cleanly."""
        assert self.run_three().is_flat


class TestIllegalTransitions:
    def test_a_second_position_is_refused(self) -> None:
        held = portfolio().enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

        with pytest.raises(PositionTransitionError, match="already holding"):
            held.enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

    def test_reversing_is_refused(self) -> None:
        held = portfolio().enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

        with pytest.raises(PositionTransitionError, match="Neither pyramiding nor reversing"):
            held.enter(intent(SignalDirection.SHORT), entry_leg(SignalDirection.SHORT))

    def test_a_refused_transition_leaves_the_cash_untouched(self) -> None:
        """The whole reason the portfolio exists: a rejected move must not have
        moved half the accounting. The book raises before the ledger is asked
        to settle anything."""
        held = portfolio().enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))
        cash_before = held.cash

        with pytest.raises(PositionTransitionError):
            held.enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

        assert held.cash == cash_before
        assert held.trades == ()

    def test_closing_while_flat_is_refused(self) -> None:
        with pytest.raises(PositionTransitionError, match="flat, so there is nothing"):
            portfolio().close(exit_leg(SignalDirection.LONG, "1420.00"))


class TestConservationAndExactness:
    def test_cash_reconciles_after_every_closed_trade(self) -> None:
        done = TestSequentialTrades().run_three()
        assert done.cash == CAPITAL + done.realized_net_pnl

    def test_conservation_holds_for_shorts_too(self) -> None:
        done = (
            portfolio()
            .enter(intent(SignalDirection.SHORT), entry_leg(SignalDirection.SHORT))
            .close(exit_leg(SignalDirection.SHORT, "1412.00"))
        )
        assert done.cash == CAPITAL + done.realized_net_pnl

    def test_prices_with_paise_stay_exact(self) -> None:
        """20.10 a share on 71 is 1427.10 exactly - no residue a float leaves."""
        done = (
            portfolio()
            .enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG, "1400.05"))
            .close(exit_leg(SignalDirection.LONG, "1420.15"))
        )

        assert done.trades[0].gross_pnl == Decimal("1427.10")
        assert done.realized_net_pnl == Decimal("1382.10")
        assert done.cash == CAPITAL + done.realized_net_pnl

    def test_the_trade_log_and_the_ledger_cannot_disagree(self) -> None:
        """Constructed directly with a mismatch, which the engine could only
        produce by settling one and not the other."""
        done = TestSequentialTrades().run_three()

        with pytest.raises(ValueError, match="cannot disagree about how many trades"):
            dataclasses.replace(done, trades=done.trades[:1])


class TestEquityCurve:
    def test_the_curve_is_chronological_and_ends_at_the_balance(self) -> None:
        done = TestSequentialTrades().run_three()
        points = done.equity_curve

        stamps = [point.at for point in points]
        assert stamps == sorted(stamps)
        assert len(points) == 4
        assert points[0].equity == CAPITAL
        assert points[-1].equity == done.cash

    def test_a_flat_portfolio_has_a_single_point(self) -> None:
        points = portfolio().equity_curve
        assert len(points) == 1
        assert points[0].at == SESSION_OPEN
        assert points[0].equity == CAPITAL

    def test_the_curve_is_derived_not_stored(self) -> None:
        """Storing it as well would be a second representation of the same
        facts, and two representations eventually disagree."""
        assert "equity_curve" not in {f.name for f in dataclasses.fields(Portfolio)}
        done = TestSequentialTrades().run_three()
        assert done.equity_curve == done.equity_curve


class TestImmutability:
    def test_every_transition_returns_a_new_portfolio(self) -> None:
        start = portfolio()
        held = start.enter(intent(SignalDirection.LONG), entry_leg(SignalDirection.LONG))

        assert start.is_flat
        assert start.cash == CAPITAL
        assert not held.is_flat
        assert held is not start

    def test_the_portfolio_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            portfolio().trades = ()  # type: ignore[misc]

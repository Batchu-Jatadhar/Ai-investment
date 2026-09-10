"""Position lifecycle: FLAT -> held -> FLAT, and the trade that falls out."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import (
    AmbiguityResolution,
    Fill,
    FillReason,
    OrderSide,
)
from app.domain.backtest.position import (
    Position,
    PositionBook,
    PositionTransitionError,
)
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN

ENTRY_BAR = SESSION_OPEN + timedelta(minutes=5)
EXIT_BAR = SESSION_OPEN + timedelta(minutes=30)


def make_intent(direction: SignalDirection = SignalDirection.LONG) -> ExecutionIntent:
    signal = Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=direction,
        stop_price=Decimal("1390.00") if direction.is_long else Decimal("1412.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=SESSION_OPEN,
        reason="test",
    )
    return ExecutionIntent(signal=signal, quantity=70, entry_bar_start=ENTRY_BAR)


def make_fill(**overrides: object) -> Fill:
    values: dict[str, object] = {
        "side": OrderSide.BUY,
        "reason": FillReason.ENTRY,
        "quantity": 70,
        "price": Decimal("1400.00"),
        "reference_price": Decimal("1399.95"),
        "slippage_per_unit": Decimal("0.05"),
        "costs": Decimal("20.00"),
        "occurred_at": ENTRY_BAR,
        "bar_start": ENTRY_BAR,
    }
    values.update(overrides)
    return Fill(**values)  # type: ignore[arg-type]


def long_entry() -> Fill:
    return make_fill()


def short_entry() -> Fill:
    return make_fill(side=OrderSide.SELL, reference_price=Decimal("1400.05"))


def long_exit() -> Fill:
    """Target hit at 1420.00 - a 20.00 move on 70 units."""
    return make_fill(
        side=OrderSide.SELL,
        reason=FillReason.TARGET,
        price=Decimal("1420.00"),
        reference_price=Decimal("1420.05"),
        costs=Decimal("25.00"),
        occurred_at=EXIT_BAR,
        bar_start=EXIT_BAR,
    )


def short_exit() -> Fill:
    """Stopped out at 1412.00 - a 12.00 move against a short."""
    return make_fill(
        side=OrderSide.BUY,
        reason=FillReason.STOP,
        price=Decimal("1412.00"),
        reference_price=Decimal("1411.95"),
        costs=Decimal("25.00"),
        occurred_at=EXIT_BAR,
        bar_start=EXIT_BAR,
    )


class TestLongRoundTrip:
    def test_flat_to_long(self) -> None:
        book = PositionBook().enter(make_intent(), long_entry())

        assert not book.is_flat
        assert book.position is not None
        assert book.position.direction is SignalDirection.LONG
        assert book.position.quantity == 70
        assert book.position.instrument_token == RELIANCE_TOKEN
        assert book.position.entry == long_entry()

    def test_long_to_flat_produces_the_trade(self) -> None:
        """20.00 a unit on 70 units is 1400.00 gross; 45.00 of costs across the
        two legs leaves 1355.00 net."""
        book = PositionBook().enter(make_intent(), long_entry())
        flat, trade = book.close(long_exit())

        assert flat.is_flat
        assert flat.position is None
        assert trade.direction is SignalDirection.LONG
        assert trade.gross_pnl == Decimal("1400.00")
        assert trade.costs == Decimal("45.00")
        assert trade.net_pnl == Decimal("1355.00")
        assert trade.exit_reason is FillReason.TARGET
        assert trade.entry == long_entry()
        assert trade.exit == long_exit()


class TestShortRoundTrip:
    def test_flat_to_short(self) -> None:
        book = PositionBook().enter(make_intent(SignalDirection.SHORT), short_entry())

        assert not book.is_flat
        assert book.position is not None
        assert book.position.direction is SignalDirection.SHORT
        assert book.position.entry.side is OrderSide.SELL

    def test_short_to_flat_loses_when_price_rises(self) -> None:
        """Sold at 1400.00, bought back at 1412.00: 12.00 a unit against, so
        -840.00 gross and -885.00 after costs. The sign is the whole point of
        keeping direction on the position."""
        book = PositionBook().enter(make_intent(SignalDirection.SHORT), short_entry())
        flat, trade = book.close(short_exit())

        assert flat.is_flat
        assert trade.gross_pnl == Decimal("-840.00")
        assert trade.net_pnl == Decimal("-885.00")
        assert trade.exit_reason is FillReason.STOP


class TestIllegalTransitions:
    def test_entering_twice_is_refused(self) -> None:
        """No pyramiding. Adding to a winner is a different strategy with a
        different risk profile."""
        book = PositionBook().enter(make_intent(), long_entry())

        with pytest.raises(PositionTransitionError, match="already holding"):
            book.enter(make_intent(), long_entry())

    def test_reversing_while_held_is_refused(self) -> None:
        """Same check: only a flat book can take a position, so flipping long
        to short without closing first cannot be expressed."""
        book = PositionBook().enter(make_intent(), long_entry())

        with pytest.raises(PositionTransitionError, match="Neither pyramiding nor reversing"):
            book.enter(make_intent(SignalDirection.SHORT), short_entry())

    def test_closing_while_flat_is_refused(self) -> None:
        with pytest.raises(PositionTransitionError, match="flat, so there is nothing"):
            PositionBook().close(long_exit())

    def test_an_exit_fill_cannot_open_a_position(self) -> None:
        with pytest.raises(PositionTransitionError, match="opened by an ENTRY fill"):
            PositionBook().enter(make_intent(), long_exit())

    def test_an_entry_fill_cannot_close_a_position(self) -> None:
        book = PositionBook().enter(make_intent(), long_entry())

        with pytest.raises(PositionTransitionError, match="closed by an exit fill"):
            book.close(long_entry())


class TestTheFillIsCheckedAgainstTheIntent:
    """A mismatch means the two halves of the engine have drifted apart, and
    finding that out here beats finding it out in a P&L number later."""

    def test_a_fill_on_the_wrong_side_is_refused(self) -> None:
        with pytest.raises(PositionTransitionError, match="enters buy, but the fill is sell"):
            PositionBook().enter(make_intent(), short_entry())

    def test_a_fill_for_the_wrong_size_is_refused(self) -> None:
        with pytest.raises(PositionTransitionError, match="the intent is for 70"):
            PositionBook().enter(make_intent(), make_fill(quantity=35))


class TestImmutabilityAndDeterminism:
    def test_a_transition_returns_a_new_book_and_leaves_the_old_one_alone(self) -> None:
        """The state is a value. A book handed to two code paths cannot be
        changed underneath either of them."""
        flat = PositionBook()
        held = flat.enter(make_intent(), long_entry())

        assert flat.is_flat
        assert not held.is_flat
        assert held is not flat

    def test_books_and_positions_are_frozen(self) -> None:
        book = PositionBook().enter(make_intent(), long_entry())

        with pytest.raises(dataclasses.FrozenInstanceError):
            book.position = None  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            book.position.quantity = 1  # type: ignore[misc, union-attr]

    def test_the_same_fills_always_produce_the_same_trade(self) -> None:
        first = PositionBook().enter(make_intent(), long_entry()).close(long_exit())
        second = PositionBook().enter(make_intent(), long_entry()).close(long_exit())
        assert first == second

    def test_a_closed_book_can_take_the_next_position(self) -> None:
        """The cycle is repeatable, which is what a session of several trades
        needs. The second entry sees no trace of the first."""
        flat, _ = PositionBook().enter(make_intent(), long_entry()).close(long_exit())
        again = flat.enter(make_intent(SignalDirection.SHORT), short_entry())

        assert again.position is not None
        assert again.position.direction is SignalDirection.SHORT


class TestTradeRecording:
    def test_the_exit_resolution_tier_reaches_the_trade(self) -> None:
        """A run where most exits fell back to the assumption is weaker than one
        where most were resolved from real data, so the tier has to survive the
        journey from the resolver to the trade log."""
        book = PositionBook().enter(make_intent(), long_entry())
        _, trade = book.close(long_exit(), ambiguity=AmbiguityResolution.PESSIMISTIC_FALLBACK)

        assert trade.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK

    def test_the_r_multiple_is_left_unset(self) -> None:
        """Whether R is measured on the gross or the net move is a reporting
        decision. Inventing one here would bake it into every trade before the
        question has been asked."""
        _, trade = PositionBook().enter(make_intent(), long_entry()).close(long_exit())
        assert trade.r_multiple is None

    def test_a_position_carries_no_unrealised_value(self) -> None:
        """What a position is currently worth depends on a price that is not
        part of it. Keeping them apart stops a stale mark being mistaken for a
        realised result - and cash and equity are the next milestone, not this
        one."""
        names = {f.name for f in dataclasses.fields(Position)}
        assert names == {"instrument_token", "direction", "quantity", "entry"}
        assert names & {"cash", "equity", "unrealised_pnl", "market_value", "last_price"} == set()

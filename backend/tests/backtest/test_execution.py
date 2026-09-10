"""The execution seam: what the simulator is asked to do, before it does it."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.time import NaiveDatetimeError
from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import OrderSide
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN

#: The signal fires on the 09:15 bar; the entry lands on the 09:20 bar.
NEXT_BAR = SESSION_OPEN + timedelta(minutes=5)


def make_signal(direction: SignalDirection = SignalDirection.LONG) -> Signal:
    return Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=direction,
        stop_price=Decimal("1390.00") if direction.is_long else Decimal("1412.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=SESSION_OPEN,
        reason="LONG_ORB_BREAKOUT" if direction.is_long else "SHORT_ORB_BREAKOUT",
    )


def make_intent(**overrides: object) -> ExecutionIntent:
    values: dict[str, object] = {
        "signal": make_signal(),
        "quantity": 70,
        "entry_bar_start": NEXT_BAR,
    }
    values.update(overrides)
    return ExecutionIntent(**values)  # type: ignore[arg-type]


class TestConstruction:
    def test_a_valid_intent_carries_the_signal_and_the_entry_bar(self) -> None:
        intent = make_intent()
        assert intent.quantity == 70
        assert intent.entry_bar_start == NEXT_BAR
        assert intent.instrument_token == RELIANCE_TOKEN
        assert intent.direction is SignalDirection.LONG

    def test_it_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            make_intent().quantity = 1  # type: ignore[misc]

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_rejected(self, quantity: int) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            make_intent(quantity=quantity)

    def test_a_naive_entry_bar_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            make_intent(entry_bar_start=datetime(2026, 8, 21, 3, 50))


class TestEntryTiming:
    """The approved model's first rule, made unrepresentable to break."""

    def test_entering_on_the_signal_bar_is_rejected(self) -> None:
        """The bar's prices have already printed by the time it closes."""
        with pytest.raises(ValueError, match="strictly after the signal bar"):
            make_intent(entry_bar_start=SESSION_OPEN)

    def test_entering_before_the_signal_bar_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly after the signal bar"):
            make_intent(entry_bar_start=SESSION_OPEN - timedelta(minutes=5))


class TestSides:
    def test_a_long_buys_in_and_sells_out(self) -> None:
        intent = make_intent()
        assert intent.entry_side is OrderSide.BUY
        assert intent.exit_side is OrderSide.SELL

    def test_a_short_sells_in_and_buys_out(self) -> None:
        intent = make_intent(signal=make_signal(SignalDirection.SHORT))
        assert intent.entry_side is OrderSide.SELL
        assert intent.exit_side is OrderSide.BUY


class TestShape:
    def test_it_holds_nothing_broker_specific(self) -> None:
        """A backtest has no orders, only assumptions about how one would have
        filled. A field that could hold a broker's order id would invite a
        live-trading path to grow through the simulator."""
        forbidden = {
            "order_id",
            "broker_order_id",
            "exchange_order_id",
            "order_type",
            "product",
            "variety",
            "account",
            "venue",
            "entry_price",
            "price",
        }
        names = {f.name for f in dataclasses.fields(ExecutionIntent)}
        assert names & forbidden == set()
        assert names == {"signal", "quantity", "entry_bar_start"}

    def test_equal_intents_compare_equal(self) -> None:
        """Value semantics, so a replayed run's intents can be diffed."""
        assert make_intent() == make_intent()
        assert make_intent() != make_intent(quantity=71)

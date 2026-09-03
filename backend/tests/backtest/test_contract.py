"""The strategy contract: what a Signal may and may not express."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.time import NaiveDatetimeError
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.contract import (
    Signal,
    SignalDirection,
    Strategy,
    StrategyContext,
)
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN, make_instrument


def long_signal(**overrides: object) -> Signal:
    values: dict[str, object] = {
        "instrument_token": RELIANCE_TOKEN,
        "direction": SignalDirection.LONG,
        "stop_price": Decimal("1395.00"),
        "target_price": Decimal("1415.00"),
        "signal_bar_start": SESSION_OPEN,
        "reason": "close above opening range high",
    }
    values.update(overrides)
    return Signal(**values)  # type: ignore[arg-type]


class TestSignalShape:
    """A Signal must be incapable of expressing what it is not allowed to decide."""

    #: Concepts a strategy must never emit. Quantity belongs to the risk engine
    #: (Phase 3); entry price is discovered by the execution simulator, because
    #: when a signal fires nobody knows the next bar's open; order identifiers
    #: and portfolio state belong to layers the strategy cannot see.
    FORBIDDEN = (
        "quantity",
        "qty",
        "size",
        "lots",
        "entry_price",
        "entry",
        "fill_price",
        "order_id",
        "order_type",
        "broker_order_id",
        "position",
        "portfolio",
        "cash",
        "equity",
    )

    def test_signal_cannot_contain_quantity_or_entry_price(self) -> None:
        names = {f.name for f in dataclasses.fields(Signal)}
        offending = sorted(names & set(self.FORBIDDEN))
        assert offending == [], f"Signal must not express: {offending}"

    def test_signal_fields_are_exactly_what_a_strategy_may_emit(self) -> None:
        assert {f.name for f in dataclasses.fields(Signal)} == {
            "instrument_token",
            "direction",
            "stop_price",
            "target_price",
            "signal_bar_start",
            "reason",
        }

    def test_constructing_with_a_quantity_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            Signal(  # type: ignore[call-arg]
                instrument_token=RELIANCE_TOKEN,
                direction=SignalDirection.LONG,
                stop_price=Decimal("1395.00"),
                target_price=Decimal("1415.00"),
                signal_bar_start=SESSION_OPEN,
                reason="x",
                quantity=10,
            )

    def test_constructing_with_an_entry_price_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            Signal(  # type: ignore[call-arg]
                instrument_token=RELIANCE_TOKEN,
                direction=SignalDirection.LONG,
                stop_price=Decimal("1395.00"),
                target_price=Decimal("1415.00"),
                signal_bar_start=SESSION_OPEN,
                reason="x",
                entry_price=Decimal("1400.00"),
            )

    def test_signal_is_frozen(self) -> None:
        signal = long_signal()
        with pytest.raises(dataclasses.FrozenInstanceError):
            signal.stop_price = Decimal("1")  # type: ignore[misc]


class TestSignalValidation:
    def test_a_valid_long_signal_is_accepted(self) -> None:
        assert long_signal().direction is SignalDirection.LONG

    def test_a_valid_short_signal_is_accepted(self) -> None:
        signal = long_signal(
            direction=SignalDirection.SHORT,
            stop_price=Decimal("1415.00"),
            target_price=Decimal("1395.00"),
        )
        assert signal.direction is SignalDirection.SHORT

    def test_long_target_below_stop_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be above its"):
            long_signal(stop_price=Decimal("1415.00"), target_price=Decimal("1395.00"))

    def test_short_target_above_stop_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be below its"):
            long_signal(
                direction=SignalDirection.SHORT,
                stop_price=Decimal("1395.00"),
                target_price=Decimal("1415.00"),
            )

    def test_naive_signal_timestamp_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            long_signal(signal_bar_start=datetime(2026, 8, 21, 3, 45))

    @pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
    def test_non_positive_stop_is_rejected(self, price: Decimal) -> None:
        with pytest.raises(ValueError, match="stop_price must be positive"):
            long_signal(stop_price=price)

    def test_float_prices_are_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            long_signal(stop_price=1395.0)

    def test_empty_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            long_signal(reason="   ")

    def test_non_positive_instrument_token_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="instrument_token must be positive"):
            long_signal(instrument_token=0)

    def test_timestamp_is_normalised_to_utc(self) -> None:
        from zoneinfo import ZoneInfo

        ist = SESSION_OPEN.astimezone(ZoneInfo("Asia/Kolkata"))
        assert long_signal(signal_bar_start=ist).signal_bar_start == SESSION_OPEN


class TestStrategyContext:
    def make(self, **overrides: object) -> StrategyContext:
        values: dict[str, object] = {
            "instrument": make_instrument(),
            "calendar": MarketSessionCalendar.nse_equity(),
            "session_open": SESSION_OPEN,
            "session_close": SESSION_OPEN + timedelta(hours=6, minutes=15),
        }
        values.update(overrides)
        return StrategyContext(**values)  # type: ignore[arg-type]

    def test_valid_context(self) -> None:
        assert self.make().instrument.tradingsymbol == "RELIANCE"

    def test_context_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            self.make().session_open = SESSION_OPEN  # type: ignore[misc]

    def test_close_before_open_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must precede"):
            self.make(session_close=SESSION_OPEN - timedelta(hours=1))

    def test_naive_bounds_are_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            self.make(session_open=datetime(2026, 8, 21, 3, 45))

    def test_context_carries_no_future_looking_fields(self) -> None:
        """Nothing here may let a strategy learn what it could not have known."""
        forbidden = {
            "repository",
            "provider",
            "clock",
            "now",
            "position",
            "portfolio",
            "future_bars",
            "all_bars",
            "next_bar",
            "trades",
        }
        names = {f.name for f in dataclasses.fields(StrategyContext)}
        assert names & forbidden == set()
        assert names == {"instrument", "calendar", "session_open", "session_close"}


class TestStrategyProtocol:
    def test_a_pure_function_object_satisfies_the_protocol(self) -> None:
        class PureStrategy:
            name = "reference"
            version = "1"

            def on_bar(self, session_bars, context):  # noqa: ANN001, ANN202
                return None

        assert isinstance(PureStrategy(), Strategy)

    def test_an_object_without_on_bar_does_not_satisfy_it(self) -> None:
        class NotAStrategy:
            name = "nope"
            version = "1"

        assert not isinstance(NotAStrategy(), Strategy)


class TestSignalDirection:
    def test_there_is_no_exit_direction(self) -> None:
        """Exits belong to the position and its levels, never to the strategy."""
        assert {d.value for d in SignalDirection} == {"long", "short"}

    def test_is_long(self) -> None:
        assert SignalDirection.LONG.is_long is True
        assert SignalDirection.SHORT.is_long is False


def test_utc_constant_is_used_by_fixtures() -> None:
    """Guards the fixture module's own assumption that SESSION_OPEN is UTC."""
    assert SESSION_OPEN.tzinfo is UTC

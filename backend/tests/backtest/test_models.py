"""Backtest result value objects and the invariants they refuse to violate."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.time import NaiveDatetimeError
from app.domain.backtest.models import (
    AmbiguityResolution,
    BacktestResult,
    EquityPoint,
    Fill,
    FillReason,
    OrderSide,
    RunManifest,
    SignalRecord,
    Trade,
)
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN

DIGEST = "a" * 64
EXIT_TIME = SESSION_OPEN + timedelta(minutes=30)


def make_fill(**overrides: object) -> Fill:
    values: dict[str, object] = {
        "side": OrderSide.BUY,
        "reason": FillReason.ENTRY,
        "quantity": 70,
        "price": Decimal("1400.05"),
        "reference_price": Decimal("1400.00"),
        "slippage_per_unit": Decimal("0.05"),
        "costs": Decimal("20.00"),
        "occurred_at": SESSION_OPEN,
        "bar_start": SESSION_OPEN,
    }
    values.update(overrides)
    return Fill(**values)  # type: ignore[arg-type]


def make_trade(**overrides: object) -> Trade:
    entry = make_fill()
    exit_fill = make_fill(
        side=OrderSide.SELL,
        reason=FillReason.TARGET,
        price=Decimal("1410.05"),
        reference_price=Decimal("1410.10"),
        costs=Decimal("25.00"),
        occurred_at=EXIT_TIME,
        bar_start=EXIT_TIME,
    )
    gross = (exit_fill.price - entry.price) * Decimal(entry.quantity)
    costs = entry.costs + exit_fill.costs
    values: dict[str, object] = {
        "instrument_token": RELIANCE_TOKEN,
        "direction": SignalDirection.LONG,
        "entry": entry,
        "exit": exit_fill,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": gross - costs,
        "exit_reason": FillReason.TARGET,
    }
    values.update(overrides)
    return Trade(**values)  # type: ignore[arg-type]


def make_signal() -> Signal:
    return Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=SignalDirection.LONG,
        stop_price=Decimal("1395.00"),
        target_price=Decimal("1415.00"),
        signal_bar_start=SESSION_OPEN,
        reason="close above opening range high",
    )


class TestEnums:
    def test_order_side_opposite(self) -> None:
        assert OrderSide.BUY.opposite is OrderSide.SELL
        assert OrderSide.SELL.opposite is OrderSide.BUY

    def test_entry_side_follows_direction(self) -> None:
        assert OrderSide.entry_for(SignalDirection.LONG) is OrderSide.BUY
        assert OrderSide.entry_for(SignalDirection.SHORT) is OrderSide.SELL

    def test_every_exit_reason_is_an_exit(self) -> None:
        assert FillReason.ENTRY.is_exit is False
        for reason in (FillReason.STOP, FillReason.TARGET, FillReason.TIME_EXIT):
            assert reason.is_exit is True

    def test_ambiguity_resolutions(self) -> None:
        assert {a.value for a in AmbiguityResolution} == {
            "unambiguous",
            "resolved_by_1m",
            "pessimistic_fallback",
        }


class TestFill:
    def test_valid_fill(self) -> None:
        assert make_fill().quantity == 70

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            make_fill().price = Decimal("1")  # type: ignore[misc]

    @pytest.mark.parametrize("quantity", [0, -5])
    def test_non_positive_quantity_is_rejected(self, quantity: int) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            make_fill(quantity=quantity)

    def test_non_positive_price_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="price must be positive"):
            make_fill(price=Decimal("0"))

    def test_negative_slippage_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="adverse by definition"):
            make_fill(slippage_per_unit=Decimal("-0.05"))

    def test_negative_costs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="costs must not be negative"):
            make_fill(costs=Decimal("-1"))

    def test_float_price_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            make_fill(price=1400.05)

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            make_fill(occurred_at=datetime(2026, 8, 21, 3, 45))


class TestTradeInvariants:
    def test_valid_trade(self) -> None:
        trade = make_trade()
        assert trade.gross_pnl == Decimal("700.00")
        assert trade.costs == Decimal("45.00")
        assert trade.net_pnl == Decimal("655.00")

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            make_trade().net_pnl = Decimal("1")  # type: ignore[misc]

    def test_net_must_equal_gross_minus_costs(self) -> None:
        with pytest.raises(ValueError, match="net_pnl .* must equal gross_pnl minus costs"):
            make_trade(net_pnl=Decimal("999.00"))

    def test_costs_must_be_the_sum_of_both_legs(self) -> None:
        with pytest.raises(ValueError, match="must be the sum of both legs"):
            make_trade(costs=Decimal("10.00"), net_pnl=Decimal("690.00"))

    def test_gross_must_match_the_fill_prices(self) -> None:
        with pytest.raises(ValueError, match="does not match the fills"):
            make_trade(gross_pnl=Decimal("800.00"), net_pnl=Decimal("755.00"))

    def test_short_gross_is_computed_the_other_way(self) -> None:
        entry = make_fill(side=OrderSide.SELL, price=Decimal("1410.00"))
        exit_fill = make_fill(
            side=OrderSide.BUY,
            reason=FillReason.TARGET,
            price=Decimal("1400.00"),
            occurred_at=EXIT_TIME,
            bar_start=EXIT_TIME,
        )
        gross = (entry.price - exit_fill.price) * Decimal(entry.quantity)
        costs = entry.costs + exit_fill.costs
        trade = Trade(
            instrument_token=RELIANCE_TOKEN,
            direction=SignalDirection.SHORT,
            entry=entry,
            exit=exit_fill,
            gross_pnl=gross,
            costs=costs,
            net_pnl=gross - costs,
            exit_reason=FillReason.TARGET,
        )
        assert trade.gross_pnl == Decimal("700.00")

    def test_long_trade_must_enter_buy(self) -> None:
        with pytest.raises(ValueError, match="must enter buy"):
            make_trade(entry=make_fill(side=OrderSide.SELL))

    def test_exit_must_be_the_opposite_side(self) -> None:
        bad_exit = make_fill(
            side=OrderSide.BUY,
            reason=FillReason.TARGET,
            price=Decimal("1410.05"),
            costs=Decimal("25.00"),
            occurred_at=EXIT_TIME,
            bar_start=EXIT_TIME,
        )
        with pytest.raises(ValueError, match="must exit sell"):
            make_trade(exit=bad_exit)

    def test_quantities_must_match_because_partials_are_not_modelled(self) -> None:
        bad_exit = make_fill(
            side=OrderSide.SELL,
            reason=FillReason.TARGET,
            quantity=35,
            price=Decimal("1410.05"),
            costs=Decimal("25.00"),
            occurred_at=EXIT_TIME,
            bar_start=EXIT_TIME,
        )
        with pytest.raises(ValueError, match="partial fills are not modelled"):
            make_trade(exit=bad_exit)

    def test_entry_fill_must_have_the_entry_reason(self) -> None:
        with pytest.raises(ValueError, match="entry fill must have reason ENTRY"):
            make_trade(entry=make_fill(reason=FillReason.STOP))

    def test_exit_fill_must_have_an_exit_reason(self) -> None:
        bad_exit = make_fill(
            side=OrderSide.SELL,
            reason=FillReason.ENTRY,
            price=Decimal("1410.05"),
            costs=Decimal("25.00"),
            occurred_at=EXIT_TIME,
            bar_start=EXIT_TIME,
        )
        with pytest.raises(ValueError, match="exit fill must have an exit reason"):
            make_trade(exit=bad_exit, exit_reason=FillReason.ENTRY)

    def test_exit_reason_must_match_the_exit_fill(self) -> None:
        with pytest.raises(ValueError, match="must match the exit fill"):
            make_trade(exit_reason=FillReason.STOP)

    def test_exit_cannot_precede_entry(self) -> None:
        early_exit = make_fill(
            side=OrderSide.SELL,
            reason=FillReason.TARGET,
            price=Decimal("1410.05"),
            costs=Decimal("25.00"),
            occurred_at=SESSION_OPEN - timedelta(minutes=5),
            bar_start=SESSION_OPEN - timedelta(minutes=5),
        )
        with pytest.raises(ValueError, match="cannot precede entry"):
            make_trade(exit=early_exit)

    def test_ambiguity_defaults_to_unambiguous(self) -> None:
        assert make_trade().ambiguity is AmbiguityResolution.UNAMBIGUOUS

    def test_r_multiple_is_optional_and_must_be_decimal(self) -> None:
        assert make_trade().r_multiple is None
        assert make_trade(r_multiple=Decimal("2")).r_multiple == Decimal("2")
        with pytest.raises(TypeError, match="never float"):
            make_trade(r_multiple=2.0)


class TestEquityPoint:
    def test_valid_point(self) -> None:
        point = EquityPoint(
            at=SESSION_OPEN,
            cash=Decimal("100000"),
            position_value=Decimal("5000"),
            equity=Decimal("105000"),
        )
        assert point.equity == Decimal("105000")

    def test_equity_must_equal_cash_plus_position(self) -> None:
        with pytest.raises(ValueError, match="must equal cash plus position_value"):
            EquityPoint(
                at=SESSION_OPEN,
                cash=Decimal("100000"),
                position_value=Decimal("5000"),
                equity=Decimal("999"),
            )

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            EquityPoint(
                at=datetime(2026, 8, 21, 3, 45),
                cash=Decimal("1"),
                position_value=Decimal("0"),
                equity=Decimal("1"),
            )


class TestSignalRecord:
    def test_defaults_to_the_strategy_stage(self) -> None:
        record = SignalRecord(signal=make_signal(), accepted=True, decision_reason="taken")
        assert record.decided_by == "strategy"

    def test_a_rejection_records_who_rejected_it(self) -> None:
        record = SignalRecord(
            signal=make_signal(),
            accepted=False,
            decision_reason="daily loss limit reached",
            decided_by="risk",
        )
        assert record.accepted is False
        assert record.decided_by == "risk"

    def test_empty_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="decision_reason must say why"):
            SignalRecord(signal=make_signal(), accepted=True, decision_reason=" ")

    def test_empty_stage_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="decided_by must name"):
            SignalRecord(signal=make_signal(), accepted=True, decision_reason="ok", decided_by="")


class TestRunManifest:
    def make(self, **overrides: object) -> RunManifest:
        values: dict[str, object] = {
            "input_fingerprint": DIGEST,
            "strategy_name": "opening_range_breakout",
            "strategy_version": "1",
            "engine_version": "2.0",
            "generated_at": SESSION_OPEN,
        }
        values.update(overrides)
        return RunManifest(**values)  # type: ignore[arg-type]

    def test_valid_manifest(self) -> None:
        assert self.make().seed is None

    @pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63])
    def test_fingerprint_must_be_a_lowercase_sha256_digest(self, bad: str) -> None:
        with pytest.raises(ValueError, match="lowercase SHA-256 hex digest"):
            self.make(input_fingerprint=bad)

    def test_identity_fields_are_required(self) -> None:
        with pytest.raises(ValueError, match="strategy_name must be recorded"):
            self.make(strategy_name="  ")

    def test_generated_at_is_on_the_manifest_not_the_fingerprint(self) -> None:
        """Wall-clock lives here so two runs of the same data stay the same run."""
        names = {f.name for f in dataclasses.fields(RunManifest)}
        assert "generated_at" in names
        assert "input_fingerprint" in names

    def test_naive_generated_at_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            self.make(generated_at=datetime(2026, 8, 21, 3, 45))


class TestBacktestResult:
    def manifest(self) -> RunManifest:
        return RunManifest(
            input_fingerprint=DIGEST,
            strategy_name="opening_range_breakout",
            strategy_version="1",
            engine_version="2.0",
            generated_at=SESSION_OPEN,
        )

    def test_defaults_are_empty_tuples(self) -> None:
        result = BacktestResult(manifest=self.manifest())
        assert result.trades == ()
        assert result.signal_log == ()
        assert result.equity_curve == ()

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            BacktestResult(manifest=self.manifest()).trades = ()  # type: ignore[misc]

    def test_lists_are_rejected_so_a_result_cannot_be_edited_later(self) -> None:
        with pytest.raises(TypeError, match="must be a tuple"):
            BacktestResult(manifest=self.manifest(), trades=[make_trade()])  # type: ignore[arg-type]

    def test_equity_curve_must_be_ordered_in_time(self) -> None:
        later = EquityPoint(
            at=EXIT_TIME, cash=Decimal("1"), position_value=Decimal("0"), equity=Decimal("1")
        )
        earlier = EquityPoint(
            at=SESSION_OPEN, cash=Decimal("1"), position_value=Decimal("0"), equity=Decimal("1")
        )
        with pytest.raises(ValueError, match="must be ordered in time"):
            BacktestResult(manifest=self.manifest(), equity_curve=(later, earlier))

    def test_an_ordered_curve_is_accepted(self) -> None:
        earlier = EquityPoint(
            at=SESSION_OPEN, cash=Decimal("1"), position_value=Decimal("0"), equity=Decimal("1")
        )
        later = EquityPoint(
            at=EXIT_TIME, cash=Decimal("2"), position_value=Decimal("0"), equity=Decimal("2")
        )
        result = BacktestResult(manifest=self.manifest(), equity_curve=(earlier, later))
        assert len(result.equity_curve) == 2

    def test_result_owns_no_calculation(self) -> None:
        """Metrics arrive in Phase 2.5; a result computes nothing."""
        public = {
            name
            for name in vars(BacktestResult)
            if not name.startswith("_") and callable(getattr(BacktestResult, name, None))
        }
        assert public == set()

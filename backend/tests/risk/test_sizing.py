"""Risk-based sizing: every limit, every boundary, every rejection worked out by hand."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal, localcontext

import pytest

from app.domain.market.models import Instrument
from app.domain.risk import sizing
from app.domain.risk.sizing import (
    LimitingConstraint,
    RejectionCode,
    RiskConfig,
    RiskDecision,
    size_position,
)
from app.domain.strategy.contract import SignalDirection

LONG = SignalDirection.LONG
SHORT = SignalDirection.SHORT

#: 1% of 10 lakh is a 10,000 budget; 500 entry with a 490 stop is 10 per unit.
CONFIG = RiskConfig(risk_per_trade_fraction=Decimal("0.01"))


def instrument(*, lot_size: int = 1, segment: str = "NSE") -> Instrument:
    return Instrument(
        instrument_token=408065,
        exchange_token=1594,
        tradingsymbol="INFY",
        name="INFOSYS",
        exchange="NSE",
        segment=segment,
        instrument_type="EQ",
        tick_size=Decimal("0.05"),
        lot_size=lot_size,
    )


def decide(
    config: RiskConfig = CONFIG,
    *,
    equity: str = "1000000",
    cash: str | None = None,
    direction: SignalDirection = LONG,
    entry: str = "500",
    stop: str = "490",
    lot_size: int = 1,
    segment: str = "NSE",
) -> RiskDecision:
    return size_position(
        config,
        equity=Decimal(equity),
        available_cash=Decimal(cash if cash is not None else equity),
        direction=direction,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        instrument=instrument(lot_size=lot_size, segment=segment),
    )


def codes(decision: RiskDecision) -> list[RejectionCode]:
    return [r.code for r in decision.rejections]


class TestApprovedSizing:
    def test_the_risk_budget_sets_the_quantity(self) -> None:
        d = decide()
        assert d.approved
        assert d.quantity == 1000
        assert d.limited_by is LimitingConstraint.RISK_BUDGET
        assert d.stop_distance == Decimal("10")
        assert d.risk_budget == Decimal("10000.00")

    def test_risk_exactly_equal_to_the_budget_is_allowed(self) -> None:
        d = decide()
        assert d.risk_amount == d.risk_budget

    def test_a_short_measures_its_stop_above_the_entry(self) -> None:
        d = decide(direction=SHORT, stop="510")
        assert (d.quantity, d.stop_distance) == (1000, Decimal("10"))

    def test_a_fractional_quotient_rounds_down(self) -> None:
        """10000 / 3 is 3333.33; rounding up would risk more than the budget."""
        d = decide(entry="100", stop="97")
        assert d.quantity == 3333
        assert d.risk_amount == Decimal("9999")

    def test_the_notional_cap_binds_before_the_risk_budget(self) -> None:
        config = RiskConfig(Decimal("0.01"), max_notional_fraction=Decimal("0.25"))
        d = decide(config)
        assert (d.quantity, d.limited_by) == (500, LimitingConstraint.NOTIONAL_LIMIT)
        assert d.notional_limit == Decimal("250000.00")

    def test_available_cash_binds_when_it_is_the_smallest_limit(self) -> None:
        d = decide(cash="300000")
        assert (d.quantity, d.limited_by) == (600, LimitingConstraint.CAPITAL)

    def test_quantity_is_a_whole_number_of_lots(self) -> None:
        """10000 / 7 is 1428 units, which is 57 lots of 25."""
        assert decide(stop="493", lot_size=25).quantity == 1425

    def test_the_minimum_quantity_itself_is_allowed(self) -> None:
        assert decide(RiskConfig(Decimal("0.01"), min_quantity=1000)).quantity == 1000

    def test_cash_for_exactly_one_unit_is_enough(self) -> None:
        d = decide(cash="500")
        assert (d.quantity, d.limited_by) == (1, LimitingConstraint.CAPITAL)

    def test_ties_report_the_limit_in_a_fixed_order(self) -> None:
        d = decide(cash="500000", stop="495")  # risk 2000, cash 1000, cap 2000
        assert d.limited_by is LimitingConstraint.CAPITAL
        d = decide(cash="1000000", stop="495")  # risk 2000 == cap 2000 == cash 2000
        assert d.limited_by is LimitingConstraint.RISK_BUDGET


class TestInvalidInputsAreRejected:
    @pytest.mark.parametrize("stop", ["500", "510"])
    def test_a_long_stop_at_or_above_the_entry(self, stop: str) -> None:
        d = decide(stop=stop)
        assert codes(d) == [RejectionCode.INVALID_STOP_DISTANCE]
        assert d.quantity == 0 and d.limited_by is None

    @pytest.mark.parametrize("stop", ["500", "490"])
    def test_a_short_stop_at_or_below_the_entry(self, stop: str) -> None:
        assert codes(decide(direction=SHORT, stop=stop)) == [RejectionCode.INVALID_STOP_DISTANCE]

    @pytest.mark.parametrize("equity", ["0", "-1", "NaN", "Infinity"])
    def test_non_positive_or_non_finite_equity(self, equity: str) -> None:
        assert codes(decide(equity=equity, cash="1000")) == [RejectionCode.INVALID_EQUITY]

    def test_negative_cash(self) -> None:
        assert codes(decide(cash="-0.01")) == [RejectionCode.INVALID_CASH]

    def test_non_positive_prices_report_every_problem(self) -> None:
        d = decide(entry="0", stop="-5")
        assert codes(d) == [
            RejectionCode.INVALID_ENTRY_PRICE,
            RejectionCode.INVALID_STOP_PRICE,
        ]

    def test_an_index_is_not_tradable(self) -> None:
        assert codes(decide(segment="INDICES")) == [RejectionCode.INVALID_INSTRUMENT]

    def test_a_meaningless_lot_size(self) -> None:
        assert codes(decide(lot_size=0)) == [RejectionCode.INVALID_INSTRUMENT]

    def test_every_rejection_explains_itself(self) -> None:
        d = decide(equity="0", cash="-1", stop="600")
        assert len(d.rejections) == 3
        assert all(r.detail.strip() for r in d.rejections)


class TestLimitsThatCannotPayForTheMinimum:
    def test_one_unit_would_exceed_the_risk_budget(self) -> None:
        """A 10,000 budget cannot carry one unit with a 10,001 stop distance."""
        d = decide(entry="20000", stop="9999", cash="1000000")
        assert codes(d) == [RejectionCode.RISK_BUDGET_EXCEEDED]
        assert d.risk_budget == Decimal("10000.00")  # still recorded for audit

    def test_the_budget_below_the_minimum_quantity(self) -> None:
        assert codes(decide(RiskConfig(Decimal("0.01"), min_quantity=1001))) == [
            RejectionCode.RISK_BUDGET_EXCEEDED
        ]

    def test_the_notional_cap_below_one_unit(self) -> None:
        config = RiskConfig(Decimal("1"), max_notional_fraction=Decimal("0.0001"))
        assert codes(decide(config, entry="500", stop="499")) == [
            RejectionCode.NOTIONAL_LIMIT_EXCEEDED
        ]

    @pytest.mark.parametrize("cash", ["0", "499.99"])
    def test_cash_short_of_one_unit(self, cash: str) -> None:
        assert codes(decide(cash=cash)) == [RejectionCode.INSUFFICIENT_CAPITAL]

    def test_cash_short_of_one_lot(self) -> None:
        """49,999 buys 99 units, which is not one 100-unit lot."""
        assert codes(decide(cash="49999", lot_size=100)) == [RejectionCode.INSUFFICIENT_CAPITAL]

    def test_every_failing_limit_is_reported_together(self) -> None:
        config = RiskConfig(Decimal("0.000001"), max_notional_fraction=Decimal("0.0001"))
        assert codes(decide(config, cash="100")) == [
            RejectionCode.RISK_BUDGET_EXCEEDED,
            RejectionCode.NOTIONAL_LIMIT_EXCEEDED,
            RejectionCode.INSUFFICIENT_CAPITAL,
        ]


class TestConfigIsNeverClamped:
    @pytest.mark.parametrize("value", ["0", "-0.01", "1.0001", "NaN"])
    @pytest.mark.parametrize("field", ["risk_per_trade_fraction", "max_notional_fraction"])
    def test_fractions_outside_zero_to_one_raise(self, field: str, value: str) -> None:
        kwargs = {"risk_per_trade_fraction": Decimal("0.01"), field: Decimal(value)}
        with pytest.raises(ValueError, match=r"must be in \(0, 1\]"):
            RiskConfig(**kwargs)  # type: ignore[arg-type]

    def test_a_float_fraction_raises(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            RiskConfig(risk_per_trade_fraction=0.01)  # type: ignore[arg-type]

    def test_a_zero_minimum_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="min_quantity must be at least 1"):
            RiskConfig(Decimal("0.01"), min_quantity=0)

    def test_float_inputs_raise(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            size_position(
                CONFIG,
                equity=1000000.0,  # type: ignore[arg-type]
                available_cash=Decimal("1000000"),
                direction=LONG,
                entry_price=Decimal("500"),
                stop_price=Decimal("490"),
                instrument=instrument(),
            )

    def test_an_inconsistent_decision_cannot_be_built(self) -> None:
        with pytest.raises(ValueError):
            RiskDecision(quantity=5)
        with pytest.raises(ValueError):
            RiskDecision(
                quantity=5,
                rejections=(sizing.RiskRejection(RejectionCode.INVALID_CASH, "x"),),
            )


class TestDeterminismAndIsolation:
    def test_the_callers_decimal_precision_cannot_change_the_answer(self) -> None:
        with localcontext() as ctx:
            ctx.prec = 3
            assert decide(entry="100", stop="97").quantity == 3333

    def test_repeated_calls_are_identical(self) -> None:
        assert len({decide(stop="493", lot_size=25) for _ in range(5)}) == 1

    def test_risk_imports_no_broker_web_ai_or_backtest_code(self) -> None:
        tree = ast.parse(inspect.getsource(sizing))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        forbidden = (
            "app.adapters",
            "app.api",
            "app.services",
            "app.domain.backtest",
            "app.domain.broker",
            "app.domain.ai",
            "fastapi",
            "httpx",
        )
        assert not {m for m in imported if m.startswith(forbidden)}

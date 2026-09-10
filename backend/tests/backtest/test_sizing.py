"""Fixed-notional sizing: the Phase 2 placeholder, with every case worked out."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from app.domain.backtest.sizing import PositionSizeError, fixed_notional_quantity
from app.domain.strategy.params import OrbParams

#: The approved Phase 2 placeholder.
NOTIONAL = Decimal("100000")


def quantity(price: str, *, lot_size: int = 1, notional: Decimal = NOTIONAL) -> int:
    return fixed_notional_quantity(Decimal(price), notional=notional, lot_size=lot_size)


class TestWholeShareQuantity:
    def test_a_normal_price_rounds_down(self) -> None:
        """100000 / 1400 is 71.428..., and the 0.428 of a share cannot be
        bought."""
        assert quantity("1400.00") == 71

    def test_a_price_that_divides_exactly_leaves_nothing_over(self) -> None:
        assert quantity("100.00") == 1000
        assert quantity("1250.00") == 80

    def test_a_long_repeating_division_still_rounds_down(self) -> None:
        """100000 / 3 is 33333.333... - the fraction is discarded, never the
        rounding of the 28th digit."""
        assert quantity("3.00") == 33333

    def test_the_execution_price_is_what_is_divided(self) -> None:
        """Slippage makes the fill dearer than the signal bar's close, and the
        notional has to cover what was really paid or the position is bigger
        than the money allowed."""
        assert quantity("1400.00") == 71
        assert quantity("1408.00") == 71
        assert quantity("1428.58") == 69


class TestLotSize:
    def test_the_result_is_a_whole_number_of_lots(self) -> None:
        """71 affordable shares is two 25-share lots, not two and five sixths."""
        assert quantity("1400.00", lot_size=25) == 50

    def test_a_lot_size_that_divides_exactly_keeps_everything(self) -> None:
        assert quantity("1000.00", lot_size=25) == 100

    def test_lot_size_one_is_the_cash_equity_case(self) -> None:
        assert quantity("1400.00", lot_size=1) == quantity("1400.00")


class TestUntradableQuantities:
    def test_a_price_above_the_notional_is_rejected(self) -> None:
        """A share dearer than the whole allowance cannot be bought at all."""
        with pytest.raises(PositionSizeError, match="cannot buy a tradable quantity"):
            quantity("150000.00")

    def test_affording_less_than_one_lot_is_rejected(self) -> None:
        """71 shares is not one 100-share lot, and a fraction of a lot is not an
        order anyone can place."""
        with pytest.raises(PositionSizeError, match="short of one 100-share lot"):
            quantity("1400.00", lot_size=100)

    def test_rejection_is_explicit_rather_than_a_zero_quantity(self) -> None:
        """A zero-share position would be accepted by everything downstream and
        reported as a trade that made exactly nothing - which reads as a
        strategy that broke even rather than as a trade that could never have
        been placed."""
        with pytest.raises(PositionSizeError):
            quantity("100001.00")


class TestArithmeticIsExact:
    def test_a_float_price_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            fixed_notional_quantity(1400.0, notional=NOTIONAL)  # type: ignore[arg-type]

    def test_a_float_notional_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            fixed_notional_quantity(Decimal("1400"), notional=100000.0)  # type: ignore[arg-type]

    @pytest.mark.parametrize("price", ["0", "-1400"])
    def test_a_non_positive_price_is_rejected(self, price: str) -> None:
        with pytest.raises(ValueError, match="price must be positive"):
            quantity(price)

    def test_a_meaningless_lot_size_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="lot_size must be at least 1"):
            quantity("1400.00", lot_size=0)

    def test_the_callers_decimal_precision_cannot_change_the_answer(self) -> None:
        """Decimal precision is process-global and mutable. Division runs in a
        pinned context so an unrelated module cannot change how many shares a
        run bought."""
        with localcontext() as ctx:
            ctx.prec = 4
            assert quantity("3.00") == 33333


class TestDeterminism:
    def test_repeated_calculation_is_identical(self) -> None:
        answers = {quantity("1400.00", lot_size=25) for _ in range(5)}
        assert answers == {50}

    def test_the_notional_matches_the_approved_hypothesis(self) -> None:
        """Guards this module's own assumption against a drift in the params."""
        assert OrbParams().fixed_notional_inr == NOTIONAL


class TestNoRiskConcepts:
    def test_sizing_takes_no_stop_no_volatility_and_no_account(self) -> None:
        """Phase 2 asks whether the strategy has an edge; Phase 3 asks what
        sizing does to it. If sizing varied here, neither answer would be
        attributable."""
        import inspect

        parameters = set(inspect.signature(fixed_notional_quantity).parameters)
        assert parameters == {"price", "notional", "lot_size"}
        assert (
            parameters
            & {
                "stop_price",
                "stop_distance",
                "atr",
                "volatility",
                "equity",
                "capital",
                "account_balance",
                "risk_per_trade",
                "risk_fraction",
            }
            == set()
        )

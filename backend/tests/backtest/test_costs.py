"""Statutory charges, hand-calculated.

Every expected figure below was worked out independently from the rates and is
asserted to the paisa. Where a rounding step matters the raw value is written
into the test so a reader can follow it without a calculator.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

import pytest

from app.domain.backtest.config import NSE_INTRADAY_EQUITY, CostSchedule
from app.domain.backtest.costs import LegCharges, leg_charges
from app.domain.backtest.models import OrderSide

SCHEDULE = NSE_INTRADAY_EQUITY


def buy(turnover: str, schedule: CostSchedule = SCHEDULE) -> LegCharges:
    return leg_charges(schedule, side=OrderSide.BUY, turnover=Decimal(turnover))


def sell(turnover: str, schedule: CostSchedule = SCHEDULE) -> LegCharges:
    return leg_charges(schedule, side=OrderSide.SELL, turnover=Decimal(turnover))


class TestBrokerage:
    def test_below_the_cap_it_is_the_percentage(self) -> None:
        """0.03% of 50,000 is 15.00, comfortably under the 20 cap."""
        assert buy("50000.00").brokerage == Decimal("15.00")

    def test_above_the_cap_it_is_the_cap(self) -> None:
        """0.03% of 100,000 is 30.00, so the 20 cap bites."""
        assert buy("100000.00").brokerage == Decimal("20.00")

    def test_at_the_crossover_both_rules_agree(self) -> None:
        """The cap and the percentage meet at 20 / 0.0003 = 66,666.66...

        Asserted as continuity rather than as a single number: whichever side of
        the boundary the turnover falls, the charge approaches 20.00 without a
        step, which is what proves the min() is the right shape.
        """
        crossover = Decimal("20") / Decimal("0.0003")

        assert buy("60000.00").brokerage == Decimal("18.00")
        assert leg_charges(SCHEDULE, side=OrderSide.BUY, turnover=crossover).brokerage == Decimal(
            "20.00"
        )
        assert buy("70000.00").brokerage == Decimal("20.00")

    def test_the_cap_is_per_leg_because_it_is_per_order(self) -> None:
        """Two legs of one round trip are two executed orders, so a round trip
        can be charged up to 40 in brokerage, never 20."""
        assert buy("100000.00").brokerage + sell("100000.00").brokerage == Decimal("40.00")


class TestSideApplicability:
    def test_stt_falls_on_the_sell_leg_only(self) -> None:
        """0.025% of 100,820 is 25.205, which rounds up to 25.21."""
        assert sell("100820.00").stt == Decimal("25.21")
        assert buy("100820.00").stt == Decimal("0.00")

    def test_stamp_duty_falls_on_the_buy_leg_only(self) -> None:
        """0.003% of 99,400 is 2.982."""
        assert buy("99400.00").stamp_duty == Decimal("2.98")
        assert sell("99400.00").stamp_duty == Decimal("0.00")

    def test_the_exchange_and_sebi_charges_fall_on_both(self) -> None:
        assert buy("99400.00").exchange_transaction == sell("99400.00").exchange_transaction
        assert buy("99400.00").sebi_turnover == sell("99400.00").sebi_turnover


class TestStatutoryRates:
    def test_the_nse_transaction_charge(self) -> None:
        """0.00307% of 99,400 is 3.05158."""
        assert buy("99400.00").exchange_transaction == Decimal("3.05")

    def test_the_sebi_turnover_fee(self) -> None:
        """Rs 10 per crore is 0.0001%; on 99,400 that is 0.0994."""
        assert buy("99400.00").sebi_turnover == Decimal("0.10")

    def test_gst_is_charged_on_the_service_charges_only(self) -> None:
        """On the sell leg the base is brokerage 20.00 + SEBI 0.10 + exchange
        3.10 = 23.20, and 18% of that is 4.176 -> 4.18.

        The discriminating part is what is *not* in the base: STT is 25.21 on
        this leg, so including it would give 8.71 instead. Getting 4.18 proves
        the tax bases are separated.
        """
        charges = sell("100820.00")

        assert charges.gst == Decimal("4.18")
        assert charges.gst == (
            (charges.brokerage + charges.sebi_turnover + charges.exchange_transaction)
            * Decimal("0.18")
        ).quantize(Decimal("0.01"))
        assert charges.gst != Decimal("8.71")


class TestWorkedRoundTrip:
    """71 shares bought at 1400.00 and sold at 1420.00, to the paisa.

    Buy leg, turnover 99,400.00:
        brokerage  0.03% = 29.82, capped              20.00
        STT        sell side only                      0.00
        exchange   0.00307% = 3.05158                  3.05
        SEBI       0.0001%  = 0.0994                   0.10
        stamp duty 0.003%   = 2.982                    2.98
        GST        18% of (20.00 + 0.10 + 3.05) = 4.167  4.17
                                                     ------
                                                      30.30

    Sell leg, turnover 100,820.00:
        brokerage  0.03% = 30.246, capped             20.00
        STT        0.025% = 25.205                    25.21
        exchange   0.00307% = 3.095174                 3.10
        SEBI       0.0001%  = 0.10082                  0.10
        stamp duty buy side only                       0.00
        GST        18% of (20.00 + 0.10 + 3.10) = 4.176  4.18
                                                     ------
                                                      52.59

    Round trip: 30.30 + 52.59 = 82.89
    """

    BUY_TURNOVER = "99400.00"
    SELL_TURNOVER = "100820.00"

    def test_the_buy_leg(self) -> None:
        charges = buy(self.BUY_TURNOVER)

        assert charges.brokerage == Decimal("20.00")
        assert charges.stt == Decimal("0.00")
        assert charges.exchange_transaction == Decimal("3.05")
        assert charges.sebi_turnover == Decimal("0.10")
        assert charges.stamp_duty == Decimal("2.98")
        assert charges.gst == Decimal("4.17")
        assert charges.total == Decimal("30.30")

    def test_the_sell_leg(self) -> None:
        charges = sell(self.SELL_TURNOVER)

        assert charges.brokerage == Decimal("20.00")
        assert charges.stt == Decimal("25.21")
        assert charges.exchange_transaction == Decimal("3.10")
        assert charges.sebi_turnover == Decimal("0.10")
        assert charges.stamp_duty == Decimal("0.00")
        assert charges.gst == Decimal("4.18")
        assert charges.total == Decimal("52.59")

    def test_the_round_trip(self) -> None:
        total = buy(self.BUY_TURNOVER).total + sell(self.SELL_TURNOVER).total
        assert total == Decimal("82.89")

    def test_the_total_is_the_sum_of_its_own_lines(self) -> None:
        """A contract note whose lines do not add up to its total is one a
        trader cannot reconcile against their broker."""
        for charges in (buy(self.BUY_TURNOVER), sell(self.SELL_TURNOVER)):
            assert charges.total == (
                charges.brokerage
                + charges.stt
                + charges.exchange_transaction
                + charges.sebi_turnover
                + charges.stamp_duty
                + charges.gst
            )


class TestScheduleMetadata:
    def test_the_schedule_is_dated_and_versioned(self) -> None:
        assert SCHEDULE.schedule_id == "nse-intraday-equity"
        assert SCHEDULE.version == "2026-03-01"
        assert SCHEDULE.effective_from == date(2026, 3, 1)

    def test_it_records_its_verification(self) -> None:
        """A schedule reporting results must say who checked it and when."""
        assert SCHEDULE.rates_verified is True
        assert SCHEDULE.verified_on == date(2026, 9, 10)
        assert SCHEDULE.source_url.startswith("https://")

    def test_a_schedule_cannot_claim_verification_without_a_source(self) -> None:
        with pytest.raises(ValueError, match="cannot claim verification without a source_url"):
            CostSchedule(
                schedule_id="x",
                version="1",
                effective_from=date(2026, 3, 1),
                verified_on=date(2026, 9, 10),
            )

    def test_an_unrated_schedule_charges_nothing(self) -> None:
        """The default is free, so rates cannot be acquired by accident."""
        placeholder = CostSchedule(
            schedule_id="placeholder", version="0", effective_from=date(2026, 1, 1)
        )
        assert placeholder.rates_verified is False
        assert buy("100000.00", placeholder).total == Decimal("0.00")

    def test_the_rates_are_part_of_the_fingerprint(self) -> None:
        """Two runs over identical bars under different schedules are different
        runs, and a fingerprint ignoring the rates would claim otherwise."""
        canonical = SCHEDULE.canonical()

        assert canonical["stt_sell_rate"] == "0.00025"
        assert canonical["exchange_transaction_rate"] == "0.0000307"
        assert canonical["gst_rate"] == "0.18"


class TestArithmeticIsExact:
    def test_a_float_turnover_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            leg_charges(SCHEDULE, side=OrderSide.BUY, turnover=99400.0)  # type: ignore[arg-type]

    def test_a_float_rate_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            CostSchedule(
                schedule_id="x",
                version="1",
                effective_from=date(2026, 3, 1),
                stt_sell_rate=0.00025,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("turnover", ["0", "-99400.00"])
    def test_a_non_positive_turnover_is_rejected(self, turnover: str) -> None:
        with pytest.raises(ValueError, match="turnover must be positive"):
            buy(turnover)

    def test_every_charge_is_a_decimal_quoted_to_the_paisa(self) -> None:
        charges = sell("100820.00")

        for value in (
            charges.brokerage,
            charges.stt,
            charges.exchange_transaction,
            charges.sebi_turnover,
            charges.stamp_duty,
            charges.gst,
        ):
            assert isinstance(value, Decimal)
            assert -value.as_tuple().exponent <= 2

    def test_the_callers_precision_cannot_change_a_charge(self) -> None:
        """Decimal precision is process-global and mutable; the calculation runs
        in a pinned context so an unrelated module cannot alter a bill."""
        with localcontext() as ctx:
            ctx.prec = 5
            assert sell("100820.00").total == Decimal("52.59")

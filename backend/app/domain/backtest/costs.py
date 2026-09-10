"""Statutory charges for one executed leg.

The arithmetic that turns a :class:`CostSchedule` into rupees. It lives here
rather than on the schedule because ``config.py`` states that its types record
what a run assumed and never compute a charge, and that separation is worth
more than the convenience of a method.

**Per leg, never per round trip.** STT falls on the sell, stamp duty on the buy,
and brokerage is capped per executed order - so a round trip is two independent
calculations added together, and its total depends on both legs' turnovers. A
single blended round-trip percentage would be wrong for every trade whose legs
differ in value, which is all of them.

.. rubric:: Rounding

Each component is rounded to the paisa as it is computed, and the total is the
sum of those rounded figures. That is how a contract note reads: every line is a
paisa figure and the lines add up to the total shown. Rounding only at the end
would produce a total that does not match its own breakdown, and a cost a trader
cannot reconcile against their broker is a cost they will not trust.

``ROUND_HALF_UP`` is used rather than banker's rounding because that is the
convention for money in this jurisdiction, and the whole calculation runs in a
pinned decimal context so a caller's precision setting cannot move a charge.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from app.domain.backtest.config import CostSchedule
from app.domain.backtest.models import OrderSide

__all__ = ["LegCharges", "leg_charges"]

#: One paisa. Every charge on a contract note is quoted to this.
_PAISA = Decimal("0.01")

#: Charges are computed here rather than in whatever context the caller has
#: installed, so an unrelated module cannot change what a run was charged.
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP)


def _to_paisa(value: Decimal) -> Decimal:
    return value.quantize(_PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class LegCharges:
    """The itemised charges on one executed leg, each to the paisa.

    Itemised rather than totalled because a single number cannot be checked. A
    reader comparing this against a real contract note needs the same lines the
    contract note has, and a discrepancy in one component is invisible inside a
    sum.
    """

    brokerage: Decimal
    stt: Decimal
    exchange_transaction: Decimal
    sebi_turnover: Decimal
    stamp_duty: Decimal
    gst: Decimal

    def __post_init__(self) -> None:
        for name in (
            "brokerage",
            "stt",
            "exchange_transaction",
            "sebi_turnover",
            "stamp_duty",
            "gst",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")

    @property
    def total(self) -> Decimal:
        """Everything charged on this leg."""
        return (
            self.brokerage
            + self.stt
            + self.exchange_transaction
            + self.sebi_turnover
            + self.stamp_duty
            + self.gst
        )


def leg_charges(
    schedule: CostSchedule,
    *,
    side: OrderSide,
    turnover: Decimal,
) -> LegCharges:
    """Charges on one executed leg of ``turnover`` rupees.

    ``turnover`` is price times quantity for that leg - the value actually
    transacted, so it uses the fill price rather than any reference level.

    Side matters twice, and only twice: STT is charged on the sell leg and stamp
    duty on the buy leg. Everything else falls on both.
    """
    if not isinstance(turnover, Decimal):
        raise TypeError("turnover must be Decimal, never float")
    if turnover <= 0:
        raise ValueError(f"turnover must be positive, got {turnover}")

    with localcontext(_CONTEXT):
        brokerage = turnover * schedule.brokerage_rate
        if schedule.brokerage_cap_inr is not None:
            brokerage = min(brokerage, schedule.brokerage_cap_inr)
        brokerage = _to_paisa(brokerage)

        is_buy = side is OrderSide.BUY
        stt = _to_paisa(Decimal(0) if is_buy else turnover * schedule.stt_sell_rate)
        stamp_duty = _to_paisa(turnover * schedule.stamp_duty_buy_rate if is_buy else Decimal(0))
        exchange_transaction = _to_paisa(turnover * schedule.exchange_transaction_rate)
        sebi_turnover = _to_paisa(turnover * schedule.sebi_turnover_rate)

        # GST applies to the service charges only. STT and stamp duty are taxes
        # in their own right and are outside its base.
        taxable = brokerage + sebi_turnover + exchange_transaction
        gst = _to_paisa(taxable * schedule.gst_rate)

    return LegCharges(
        brokerage=brokerage,
        stt=stt,
        exchange_transaction=exchange_transaction,
        sebi_turnover=sebi_turnover,
        stamp_duty=stamp_duty,
        gst=gst,
    )

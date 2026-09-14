"""Risk-based position sizing: how many units a proposed entry may trade, or why none.

.. rubric:: What this decides, and what it does not

Handed an account's equity and cash, a proposed entry and stop, and the
instrument, :func:`size_position` returns a :class:`RiskDecision`: either an
approved whole quantity or a rejection carrying every reason it failed. It does
not generate signals, does not see a broker, and places nothing. It is a pure
function of its arguments and its :class:`RiskConfig`.

.. rubric:: The rule

::

    risk budget      = equity * risk_per_trade_fraction
    stop distance    = entry - stop  (long)   |   stop - entry  (short)
    quantity by risk = floor(budget / stop distance)
    quantity by cap  = floor(equity * max_notional_fraction / entry)
    quantity by cash = floor(available cash / entry)
    quantity         = min of the three, rounded down to a whole lot

Every limit rounds **down**, so the approved position can never lose more than
the budget at its stop, never exceed the notional cap, and never cost more than
the cash on hand. Rounding down to a smaller position is sizing, not clamping:
the decision names the constraint that bound it in ``limited_by``. What is never
done is to round *up*, or to invent a quantity when a limit cannot pay for the
minimum tradable size - that is a rejection, with the limit that failed named.

Invalid inputs (non-positive equity or prices, negative cash, a stop on the
wrong side of the entry, an untradable instrument) are rejections too, reported
before any arithmetic runs. Floats and a nonsensical :class:`RiskConfig` raise,
because those are programming errors rather than trade outcomes.

ponytail: no costs, slippage, margin or leverage in the per-unit risk; the
notional cap is at most 100% of equity for that reason. Add them when a margin
model exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum

from app.domain.market.models import Instrument
from app.domain.strategy.contract import SignalDirection

__all__ = [
    "LimitingConstraint",
    "RejectionCode",
    "RiskConfig",
    "RiskDecision",
    "RiskRejection",
    "size_position",
]

#: Pinned for the same reason as the Phase 2 sizing module: Decimal precision is
#: process-global, and a caller's context must not change a position size.
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)

_ONE = Decimal(1)


class RejectionCode(StrEnum):
    """Machine-readable reason a proposal was not approved."""

    INVALID_EQUITY = "invalid_equity"
    INVALID_CASH = "invalid_cash"
    INVALID_ENTRY_PRICE = "invalid_entry_price"
    INVALID_STOP_PRICE = "invalid_stop_price"
    INVALID_STOP_DISTANCE = "invalid_stop_distance"
    INVALID_INSTRUMENT = "invalid_instrument"
    #: One minimum-size position at this stop would lose more than the budget.
    RISK_BUDGET_EXCEEDED = "risk_budget_exceeded"
    #: One minimum-size position would exceed the notional exposure cap.
    NOTIONAL_LIMIT_EXCEEDED = "notional_limit_exceeded"
    #: The cash on hand cannot pay for one minimum-size position.
    INSUFFICIENT_CAPITAL = "insufficient_capital"


class LimitingConstraint(StrEnum):
    """Which limit set the approved quantity."""

    RISK_BUDGET = "risk_budget"
    NOTIONAL_LIMIT = "notional_limit"
    CAPITAL = "capital"


@dataclass(frozen=True, slots=True)
class RiskRejection:
    code: RejectionCode
    detail: str


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """The limits a proposal is sized against. Invalid limits raise, never clamp.

    ``risk_per_trade_fraction`` is the share of equity one trade may lose at its
    stop. ``max_notional_fraction`` caps one position's notional as a share of
    equity, and may not exceed 1 because leverage is not modelled.
    ``min_quantity`` is the smallest position worth placing, in units.
    """

    risk_per_trade_fraction: Decimal
    max_notional_fraction: Decimal = _ONE
    min_quantity: int = 1

    def __post_init__(self) -> None:
        for name in ("risk_per_trade_fraction", "max_notional_fraction"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
            if not value.is_finite() or not Decimal(0) < value <= _ONE:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        if isinstance(self.min_quantity, bool) or not isinstance(self.min_quantity, int):
            raise TypeError("min_quantity must be int")
        if self.min_quantity < 1:
            raise ValueError(f"min_quantity must be at least 1, got {self.min_quantity}")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """An approved quantity, or the reasons there is none.

    The computed figures are recorded whenever the inputs were valid enough to
    compute them, so a rejection can be audited as easily as an approval.
    """

    quantity: int
    rejections: tuple[RiskRejection, ...] = ()
    stop_distance: Decimal | None = None
    risk_budget: Decimal | None = None
    notional_limit: Decimal | None = None
    limited_by: LimitingConstraint | None = None

    def __post_init__(self) -> None:
        if self.rejections and (self.quantity != 0 or self.limited_by is not None):
            raise ValueError("a rejected decision carries no quantity and no limiting constraint")
        if not self.rejections and (self.quantity <= 0 or self.limited_by is None):
            raise ValueError("an approved decision needs a positive quantity and its binding limit")

    @property
    def approved(self) -> bool:
        return not self.rejections

    @property
    def risk_amount(self) -> Decimal | None:
        """What the approved position loses if it exits exactly at its stop."""
        if not self.approved or self.stop_distance is None:
            return None
        return self.stop_distance * self.quantity


def _require_decimal(**values: Decimal) -> None:
    for name, value in values.items():
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal, never float")


def size_position(
    config: RiskConfig,
    *,
    equity: Decimal,
    available_cash: Decimal,
    direction: SignalDirection,
    entry_price: Decimal,
    stop_price: Decimal,
    instrument: Instrument,
) -> RiskDecision:
    """Size a proposed entry against ``config``, or reject it with every reason."""
    _require_decimal(
        equity=equity, available_cash=available_cash, entry_price=entry_price, stop_price=stop_price
    )

    invalid: list[RiskRejection] = []
    if not equity.is_finite() or equity <= 0:
        invalid.append(RiskRejection(RejectionCode.INVALID_EQUITY, f"equity is {equity}"))
    if not available_cash.is_finite() or available_cash < 0:
        invalid.append(
            RiskRejection(RejectionCode.INVALID_CASH, f"available cash is {available_cash}")
        )
    if not entry_price.is_finite() or entry_price <= 0:
        invalid.append(
            RiskRejection(RejectionCode.INVALID_ENTRY_PRICE, f"entry price is {entry_price}")
        )
    if not stop_price.is_finite() or stop_price <= 0:
        invalid.append(
            RiskRejection(RejectionCode.INVALID_STOP_PRICE, f"stop price is {stop_price}")
        )
    if not instrument.is_tradable or instrument.lot_size < 1:
        invalid.append(
            RiskRejection(
                RejectionCode.INVALID_INSTRUMENT,
                f"{instrument.key} is not tradable with lot size {instrument.lot_size}",
            )
        )
    stop_distance: Decimal | None = None
    prices_valid = not {r.code for r in invalid} & {
        RejectionCode.INVALID_ENTRY_PRICE,
        RejectionCode.INVALID_STOP_PRICE,
    }
    if prices_valid:
        stop_distance = entry_price - stop_price if direction.is_long else stop_price - entry_price
        if stop_distance <= 0:
            invalid.append(
                RiskRejection(
                    RejectionCode.INVALID_STOP_DISTANCE,
                    f"a {direction.value} entry at {entry_price} with its stop at {stop_price} "
                    f"has stop distance {stop_distance}; the stop must be on the losing side",
                )
            )
    if invalid:
        return RiskDecision(quantity=0, rejections=tuple(invalid))
    assert stop_distance is not None

    lot = instrument.lot_size
    with localcontext(_CONTEXT):
        risk_budget = equity * config.risk_per_trade_fraction
        notional_limit = equity * config.max_notional_fraction
        # ``//`` is exact integer division, so no 28th-digit rounding can lift a
        # quotient of 99.999... to 100.
        limits = {
            LimitingConstraint.RISK_BUDGET: int(risk_budget // stop_distance) // lot * lot,
            LimitingConstraint.NOTIONAL_LIMIT: int(notional_limit // entry_price) // lot * lot,
            LimitingConstraint.CAPITAL: int(available_cash // entry_price) // lot * lot,
        }

    minimum = config.min_quantity
    failures = {
        LimitingConstraint.RISK_BUDGET: (
            RejectionCode.RISK_BUDGET_EXCEEDED,
            f"budget {risk_budget} at stop distance {stop_distance} allows "
            f"{limits[LimitingConstraint.RISK_BUDGET]} unit(s)",
        ),
        LimitingConstraint.NOTIONAL_LIMIT: (
            RejectionCode.NOTIONAL_LIMIT_EXCEEDED,
            f"notional limit {notional_limit} at {entry_price} allows "
            f"{limits[LimitingConstraint.NOTIONAL_LIMIT]} unit(s)",
        ),
        LimitingConstraint.CAPITAL: (
            RejectionCode.INSUFFICIENT_CAPITAL,
            f"cash {available_cash} at {entry_price} buys "
            f"{limits[LimitingConstraint.CAPITAL]} unit(s)",
        ),
    }
    rejections = tuple(
        RiskRejection(code, f"{detail}, below the minimum quantity {minimum} (lot size {lot})")
        for constraint, (code, detail) in failures.items()
        if limits[constraint] < minimum or limits[constraint] == 0
    )
    if rejections:
        return RiskDecision(
            quantity=0,
            rejections=rejections,
            stop_distance=stop_distance,
            risk_budget=risk_budget,
            notional_limit=notional_limit,
        )

    # min() keeps the first of equal values, so ties report in a fixed order.
    limited_by = min(limits, key=limits.__getitem__)
    return RiskDecision(
        quantity=limits[limited_by],
        stop_distance=stop_distance,
        risk_budget=risk_budget,
        notional_limit=notional_limit,
        limited_by=limited_by,
    )

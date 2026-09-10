"""Phase 2 position sizing: a fixed notional, and nothing cleverer.

.. rubric:: This is a PLACEHOLDER, not the risk engine

Every position in Phase 2 is the same rupee amount, ``fixed_notional_inr``,
divided by the price it actually filled at. That is deliberately the dullest
rule available, and it is dull for a reason: Phase 2 asks whether the strategy
has an edge, and Phase 3 asks what sizing does to it. If sizing varied here,
the two questions would be answered at once and neither answer would be
attributable.

So there is **no stop-distance sizing, no volatility targeting, no fraction of
capital at risk, and no account balance**. Those arrive in Phase 3, in their own
module, and the comparison between the two phases is only meaningful because
nothing resembling them is smuggled in now. This module cannot even see an
account: it is handed a price, a notional and a lot size, and returns a whole
number of shares.

Rounding is always **down**. A position that rounds up is one the notional could
not actually have paid for, and financing the difference is a leverage
assumption nobody made.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

__all__ = ["PositionSizeError", "fixed_notional_quantity"]

#: Division runs here rather than in whatever context the caller installed.
#: Decimal precision is process-global and mutable, so an unpinned context would
#: let an unrelated module change how many shares a run bought.
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class PositionSizeError(ValueError):
    """Raised when the notional cannot buy a tradable quantity.

    A ``ValueError`` subclass, following ``PositionTransitionError`` and
    ``UnexecutableBarError``, so a caller can catch it precisely rather than
    matching on message text.

    Returning ``0`` instead would be worse than raising. A zero-share position
    is not a position, and every downstream calculation would happily accept it
    and report a trade that made exactly nothing - which reads as a strategy
    that broke even rather than as a trade that could never have been placed.
    """


def fixed_notional_quantity(
    price: Decimal,
    *,
    notional: Decimal,
    lot_size: int = 1,
) -> int:
    """Whole shares of ``price`` that ``notional`` buys, rounded down to a lot.

    ``price`` is the price the position actually executes at, not the signal
    bar's close: the notional has to cover what was really paid, including
    slippage, or the position is bigger than the money allowed.

    ``lot_size`` is the instrument's tradable unit - 1 for NSE cash equity, the
    contract size for a derivative. The result is always a whole multiple of it,
    because a fraction of a lot is not an order anyone can place.

    Raises :class:`PositionSizeError` when the notional cannot buy a single lot.
    """
    for name, value in (("price", price), ("notional", notional)):
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal, never float")
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    if lot_size < 1:
        raise ValueError(f"lot_size must be at least 1, got {lot_size}")

    with localcontext(_CONTEXT):
        affordable = int(notional / price)

    lots = affordable // lot_size
    quantity = lots * lot_size

    if quantity <= 0:
        detail = (
            f"{notional} buys {affordable} share(s) at {price}"
            if lot_size == 1
            else f"{notional} buys {affordable} share(s) at {price}, short of one "
            f"{lot_size}-share lot"
        )
        raise PositionSizeError(
            f"the fixed notional cannot buy a tradable quantity: {detail}. This instrument "
            "cannot be traded at this notional, which is a fact about the pair rather than a "
            "position of size zero"
        )

    return quantity

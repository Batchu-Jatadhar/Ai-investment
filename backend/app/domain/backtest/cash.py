"""Realized cash accounting.

What the account actually holds, moved by the fills that happened. Starting
capital is stated rather than assumed, every movement comes from a
:class:`Fill`, and the running totals are derived from :class:`Trade` objects
that have already validated their own arithmetic.

**Cash flow depends on the side, not the direction.** A buy pays out and a sell
takes in, whether that buy is opening a long or closing a short, and costs
always reduce cash. That single rule produces the correct signs for all four
cases without any of them being special:

======================  =========================  =========================
Trade                   Entry leg                  Exit leg
======================  =========================  =========================
long                    buy: cash out              sell: cash in
short                   sell: cash in              buy: cash out
======================  =========================  =========================

and in every case the round trip leaves ``cash`` exactly
``starting_capital + gross - costs``. That identity is the point of this module,
and it is what a later equity curve will be built on.

**No hidden state.** The ledger is a frozen value; every movement returns a new
one. Nothing accumulates in an object that two code paths might share.

Deliberately absent: performance metrics, drawdown, and any mark to market. A
ledger knows what was realized, never what an open position might currently be
worth - that needs a price which is not part of the ledger, and conflating the
two is how an unrealized number ends up reported as a result.

.. rubric:: The equity curve

:func:`build_equity_curve` turns a run's trades into a timestamped series of
:class:`EquityPoint`.

**It samples only when the book is flat**, and that is the whole design. There
is no approved model for valuing a position that is still held - marking to the
last close, to the bar's midpoint, or to the entry are three different curves
from the same trades, and choosing between them is a decision this milestone
was told not to invent. Sampling at realizations sidesteps it completely:
between a trade closing and the next one opening nothing is held, so
``position_value`` is zero as a matter of fact rather than as an assumption, and
``equity`` is simply the cash.

The cost is that the curve says nothing about what happened *inside* a trade,
so an intra-trade excursion is invisible to it. That matters for drawdown, which
is exactly why drawdown is not built on this yet: when it is, it will need an
explicit valuation rule, and that rule should be chosen deliberately rather than
inherited by accident from whatever this function happened to do.

Timestamps come from the fills. Nothing here reads a clock, so the same trades
always produce the same curve.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.time import ensure_utc
from app.domain.backtest.models import EquityPoint, Fill, FillReason, OrderSide, Trade

__all__ = ["CashLedger", "build_equity_curve"]


def _cash_delta(fill: Fill) -> Decimal:
    """How much this fill moves cash, signed.

    A buy pays out the consideration, a sell takes it in, and the costs of
    either come off the top.
    """
    consideration = fill.price * Decimal(fill.quantity)
    if fill.side is OrderSide.BUY:
        return -consideration - fill.costs
    return consideration - fill.costs


@dataclass(frozen=True, slots=True)
class CashLedger:
    """Cash held, and what has been realized so far.

    ``cash`` is the balance after every fill applied to date, including while a
    position is held - a long's entry has already been paid for, so the balance
    is genuinely lower until it is closed. ``starting_capital + realized_net_pnl``
    equals ``cash`` whenever nothing is open, and that reconciliation is the
    invariant worth asserting in tests.

    ponytail: no margin model. A notional larger than the balance will drive
    cash negative rather than being refused, because refusing it correctly needs
    the intraday leverage rules, and inventing those would be a worse error than
    leaving the gap visible. Capital adequacy belongs to the Phase 3 risk engine.
    """

    starting_capital: Decimal
    cash: Decimal
    realized_gross_pnl: Decimal = Decimal(0)
    realized_costs: Decimal = Decimal(0)
    closed_trades: int = 0

    def __post_init__(self) -> None:
        for name in ("starting_capital", "cash", "realized_gross_pnl", "realized_costs"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        if self.starting_capital <= 0:
            raise ValueError(
                f"starting_capital must be positive, got {self.starting_capital}; a run has to "
                "state what it began with"
            )
        if self.realized_costs < 0:
            raise ValueError(f"realized_costs must not be negative, got {self.realized_costs}")
        if self.closed_trades < 0:
            raise ValueError(f"closed_trades must not be negative, got {self.closed_trades}")

    @classmethod
    def funded(cls, starting_capital: Decimal) -> CashLedger:
        """A ledger holding its starting capital and nothing realized yet."""
        return cls(starting_capital=starting_capital, cash=starting_capital)

    @property
    def realized_net_pnl(self) -> Decimal:
        """Gross less costs. What the closed trades actually kept."""
        return self.realized_gross_pnl - self.realized_costs

    def after_entry(self, fill: Fill) -> CashLedger:
        """Apply an entry leg. Moves cash; realizes nothing.

        Opening a position settles no profit or loss - it converts cash into a
        holding. Counting anything as realized here would book a result before
        the trade had one.
        """
        if fill.reason is not FillReason.ENTRY:
            raise ValueError(
                f"after_entry needs an ENTRY fill, got {fill.reason.value}; an exit leg also "
                "realizes a trade and must go through after_exit"
            )
        return CashLedger(
            starting_capital=self.starting_capital,
            cash=self.cash + _cash_delta(fill),
            realized_gross_pnl=self.realized_gross_pnl,
            realized_costs=self.realized_costs,
            closed_trades=self.closed_trades,
        )

    def after_exit(self, trade: Trade) -> CashLedger:
        """Apply an exit leg and realize the trade it completed.

        Takes the whole :class:`Trade` rather than just its exit fill, because
        the exit is the moment both are known and :class:`Trade` has already
        checked that its gross, costs and net agree with the two fills. Deriving
        the realized totals a second time here would be a second chance to get
        them wrong.
        """
        return CashLedger(
            starting_capital=self.starting_capital,
            cash=self.cash + _cash_delta(trade.exit),
            realized_gross_pnl=self.realized_gross_pnl + trade.gross_pnl,
            realized_costs=self.realized_costs + trade.costs,
            closed_trades=self.closed_trades + 1,
        )


#: A flat book holds nothing, so there is nothing to value.
_NOTHING_HELD = Decimal(0)


def build_equity_curve(
    trades: Sequence[Trade],
    *,
    starting_capital: Decimal,
    start_at: datetime,
) -> tuple[EquityPoint, ...]:
    """The equity series for ``trades``, oldest first.

    The first point is the run's opening balance at ``start_at`` - supplied by
    the caller from the data, typically the first session's open, because
    nothing here may read a clock. Every later point is a trade closing, stamped
    with the moment its exit filled.

    ``trades`` must be in non-decreasing exit order. They are validated rather
    than sorted: trades arriving out of order means the engine produced them out
    of order, and quietly re-sorting would hide that while still producing a
    plausible curve.

    Cash is carried by :class:`CashLedger` rather than recomputed, so the curve
    and the ledger can never disagree about what a run is worth. Both legs of
    each trade are applied, because a round trip's cash effect is the entry and
    the exit together.
    """
    if not isinstance(starting_capital, Decimal):
        raise TypeError("starting_capital must be Decimal, never float")

    ledger = CashLedger.funded(starting_capital)
    at = ensure_utc(start_at)
    points = [
        EquityPoint(
            at=at,
            cash=ledger.cash,
            position_value=_NOTHING_HELD,
            equity=ledger.cash,
        )
    ]

    for index, trade in enumerate(trades):
        closed_at = ensure_utc(trade.exit.occurred_at)
        if closed_at < at:
            raise ValueError(
                f"trade {index} closed at {closed_at.isoformat()}, before the previous point at "
                f"{at.isoformat()}; an equity curve is a series in time and its inputs must "
                "already be in the order they happened"
            )
        ledger = ledger.after_entry(trade.entry).after_exit(trade)
        at = closed_at
        points.append(
            EquityPoint(
                at=at,
                cash=ledger.cash,
                position_value=_NOTHING_HELD,
                equity=ledger.cash,
            )
        )

    return tuple(points)

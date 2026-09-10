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
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.backtest.models import Fill, FillReason, OrderSide, Trade

__all__ = ["CashLedger"]


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

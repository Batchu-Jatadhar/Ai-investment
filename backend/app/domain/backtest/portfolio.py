"""The portfolio: one object that keeps the accounting pieces in step.

Phase 2.4 built four components that each know one thing well - the position
lifecycle, fixed-notional sizing, the cash ledger, and the equity curve. Left
separate they are easy to test and easy to get out of step: a caller that closed
a position but forgot to settle the cash would produce a trade log and a balance
that disagree, and nothing would say so.

:class:`Portfolio` is the seam that makes that impossible. Every transition
moves the book and the ledger **together**, in one call, so they cannot drift.

**It coordinates and does not decide.** There is no execution logic here - it is
handed fills, it does not resolve them. No strategy logic - it is handed
intents, it does not generate them. No risk logic - Phase 2 sizing is one
division, and capital adequacy is Phase 3. No metrics formulas - the equity
curve is delegated, and Sharpe, drawdown and the rest are Phase 2.5. Every
method here is a few lines of delegation, and that is the point rather than a
shortcoming.

**Immutable throughout.** Each transition returns a new portfolio, so a run
cannot be half-applied and a portfolio handed to two code paths cannot be
changed underneath either of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.time import ensure_utc
from app.domain.backtest.cash import CashLedger, build_equity_curve
from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import AmbiguityResolution, EquityPoint, Fill, Trade
from app.domain.backtest.position import PositionBook
from app.domain.backtest.sizing import fixed_notional_quantity

__all__ = ["Portfolio"]


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Cash, the held position, and the trades closed so far.

    ``started_at`` is the moment the equity curve begins, supplied from the
    data rather than from a clock. ``fixed_notional`` is the Phase 2 sizing
    placeholder, held here because the portfolio owns the capital and therefore
    owns how much of it one position uses.
    """

    ledger: CashLedger
    started_at: datetime
    fixed_notional: Decimal
    book: PositionBook = field(default_factory=PositionBook)
    trades: tuple[Trade, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        if not isinstance(self.fixed_notional, Decimal):
            raise TypeError("fixed_notional must be Decimal, never float")
        if self.fixed_notional <= 0:
            raise ValueError(f"fixed_notional must be positive, got {self.fixed_notional}")
        if len(self.trades) != self.ledger.closed_trades:
            raise ValueError(
                f"{len(self.trades)} trade(s) recorded but the ledger has settled "
                f"{self.ledger.closed_trades}; the trade log and the cash balance describe the "
                "same run and cannot disagree about how many trades it made"
            )

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    @classmethod
    def funded(
        cls,
        starting_capital: Decimal,
        *,
        started_at: datetime,
        fixed_notional: Decimal,
    ) -> Portfolio:
        """A flat portfolio holding its starting capital."""
        return cls(
            ledger=CashLedger.funded(starting_capital),
            started_at=started_at,
            fixed_notional=fixed_notional,
        )

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    @property
    def is_flat(self) -> bool:
        return self.book.is_flat

    @property
    def cash(self) -> Decimal:
        return self.ledger.cash

    @property
    def realized_net_pnl(self) -> Decimal:
        return self.ledger.realized_net_pnl

    @property
    def equity_curve(self) -> tuple[EquityPoint, ...]:
        """The curve, derived from the trade log rather than accumulated.

        Storing it as well would be a second representation of the same facts,
        and two representations of the same facts eventually disagree.
        """
        return build_equity_curve(
            self.trades,
            starting_capital=self.ledger.starting_capital,
            start_at=self.started_at,
        )

    # ------------------------------------------------------------------ #
    # sizing
    # ------------------------------------------------------------------ #

    def size_for(self, price: Decimal, *, lot_size: int = 1) -> int:
        """Whole shares the fixed notional buys at ``price``.

        Delegated in full. The portfolio decides *how much notional*, and the
        sizing module decides how that becomes a share count - it does not see
        the balance, which is what keeps Phase 2 sizing independent of how the
        run happens to be going.
        """
        return fixed_notional_quantity(price, notional=self.fixed_notional, lot_size=lot_size)

    # ------------------------------------------------------------------ #
    # transitions
    # ------------------------------------------------------------------ #

    def enter(self, intent: ExecutionIntent, entry_fill: Fill) -> Portfolio:
        """Take a position and pay for it, in one step.

        Refuses a second position and a reversal, because the book does - the
        rule lives in one place and this inherits it rather than restating it.
        """
        return Portfolio(
            ledger=self.ledger.after_entry(entry_fill),
            started_at=self.started_at,
            fixed_notional=self.fixed_notional,
            book=self.book.enter(intent, entry_fill),
            trades=self.trades,
        )

    def close(
        self,
        exit_fill: Fill,
        *,
        ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS,
    ) -> Portfolio:
        """Close the held position, settle the cash, and record the trade.

        The book builds the trade and the ledger settles it from that same
        trade, so the balance and the log are two views of one event rather than
        two calculations that have to agree.
        """
        book, trade = self.book.close(exit_fill, ambiguity=ambiguity)
        return Portfolio(
            ledger=self.ledger.after_exit(trade),
            started_at=self.started_at,
            fixed_notional=self.fixed_notional,
            book=book,
            trades=(*self.trades, trade),
        )

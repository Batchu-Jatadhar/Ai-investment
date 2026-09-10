"""Position lifecycle.

The state machine between fills and trades, and nothing else::

    FLAT --enter--> LONG or SHORT --close--> FLAT

It consumes the :class:`Fill` objects the execution simulator produces and
emits a :class:`Trade` when a position closes. It does not price anything, size
anything, or total anything up: cash, equity and portfolio-wide accounting are
the next milestone, and performance metrics the one after.

**The state lives in a value, not in an object that mutates.** Every transition
returns a new :class:`PositionBook`, so a caller cannot half-apply one, and a
book handed to two code paths cannot be changed underneath either of them. That
is what makes replaying a session reproducible rather than dependent on the
order things happened to be called in.

.. rubric:: Two rules the type enforces rather than documents

*   **No pyramiding.** A book holding a position refuses to take another. Adding
    to a winner is a different strategy with a different risk profile, and the
    hypothesis under test does not do it.
*   **No reversal while a position is held.** Flipping long to short without
    closing first is the same refusal, which is why both are one check: the book
    is either flat or it is not, and only a flat book can take a position.

Both raise :class:`PositionTransitionError`. Silently ignoring the second entry,
or silently closing the first, would produce a trade log that no longer matches
the fills that made it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.backtest.execution import ExecutionIntent
from app.domain.backtest.models import AmbiguityResolution, Fill, FillReason, Trade
from app.domain.strategy.contract import SignalDirection

__all__ = ["Position", "PositionBook", "PositionTransitionError"]


class PositionTransitionError(ValueError):
    """Raised when a transition the lifecycle does not have is attempted.

    A ``ValueError`` subclass, following the pattern of
    ``InvalidBacktestInputError`` and ``UnexecutableBarError``, so a caller can
    catch it precisely rather than matching on message text.
    """


@dataclass(frozen=True, slots=True)
class Position:
    """One held position, and the fill that opened it.

    Carries no unrealised value and no mark to market. What a position is
    currently worth depends on a price that is not part of the position, and
    keeping the two apart is what stops a stale mark being mistaken for a
    realised result.
    """

    instrument_token: int
    direction: SignalDirection
    quantity: int
    entry: Fill

    def __post_init__(self) -> None:
        if self.instrument_token <= 0:
            raise ValueError(f"instrument_token must be positive, got {self.instrument_token}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.entry.reason is not FillReason.ENTRY:
            raise ValueError(
                f"a position is opened by an ENTRY fill, got {self.entry.reason.value}"
            )
        if self.entry.quantity != self.quantity:
            raise ValueError(
                f"position quantity ({self.quantity}) must match its entry fill "
                f"({self.entry.quantity}); partial fills are not modelled in Phase 2"
            )


@dataclass(frozen=True, slots=True)
class PositionBook:
    """Flat, or holding exactly one position, for one instrument.

    One instrument per book by design. A multi-instrument run is a book each,
    which keeps every transition a local decision and means one instrument's
    state can never be confused for another's.
    """

    position: Position | None = None

    @property
    def is_flat(self) -> bool:
        return self.position is None

    def enter(self, intent: ExecutionIntent, entry_fill: Fill) -> PositionBook:
        """Take the position ``intent`` describes, filled by ``entry_fill``.

        Raises :class:`PositionTransitionError` if the book already holds one -
        which is both the no-pyramiding rule and the no-reversal rule, because
        neither is available from a book that is not flat.

        The fill is checked against the intent rather than trusted. A fill on
        the wrong side, or for the wrong size, means the two halves of the
        engine have drifted apart, and finding that out here beats finding it
        out in a P&L number three milestones later.
        """
        held = self.position
        if held is not None:
            raise PositionTransitionError(
                f"already holding {held.quantity} {held.direction.value} of instrument "
                f"{held.instrument_token}; a position must be closed before another is taken. "
                f"Neither pyramiding nor reversing into {intent.direction.value} is part of "
                "this hypothesis"
            )
        if entry_fill.reason is not FillReason.ENTRY:
            raise PositionTransitionError(
                f"a position is opened by an ENTRY fill, got {entry_fill.reason.value}"
            )
        if entry_fill.side is not intent.entry_side:
            raise PositionTransitionError(
                f"a {intent.direction.value} enters {intent.entry_side.value}, but the fill is "
                f"{entry_fill.side.value}"
            )
        if entry_fill.quantity != intent.quantity:
            raise PositionTransitionError(
                f"the intent is for {intent.quantity} but the fill is for "
                f"{entry_fill.quantity}; partial fills are not modelled in Phase 2"
            )

        return PositionBook(
            Position(
                instrument_token=intent.instrument_token,
                direction=intent.direction,
                quantity=intent.quantity,
                entry=entry_fill,
            )
        )

    def close(
        self,
        exit_fill: Fill,
        *,
        ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS,
    ) -> tuple[PositionBook, Trade]:
        """Close the held position, returning the flat book and the trade.

        The arithmetic is the round trip and nothing more: gross is the price
        move times the quantity, signed by direction; costs are the two legs
        summed; net is gross less costs. :class:`Trade` re-checks each of those
        and refuses a result that does not add up, so a mistake here fails
        immediately rather than becoming a plausible-looking number.

        ``ambiguity`` is carried through from the exit resolution so the trade
        records whether its exit was decided by real 1-minute data or by the
        pessimistic assumption.

        ``r_multiple`` is deliberately left unset. Whether R is measured on the
        gross or the net move is a reporting decision, and inventing one here
        would bake it into every trade before the question has been asked.
        """
        held = self.position
        if held is None:
            raise PositionTransitionError(
                f"flat, so there is nothing for this {exit_fill.reason.value} fill to close"
            )
        if not exit_fill.reason.is_exit:
            raise PositionTransitionError(
                f"a position is closed by an exit fill, got {exit_fill.reason.value}"
            )

        quantity = Decimal(held.quantity)
        move = (
            exit_fill.price - held.entry.price
            if held.direction.is_long
            else held.entry.price - exit_fill.price
        )
        gross = move * quantity
        costs = held.entry.costs + exit_fill.costs

        trade = Trade(
            instrument_token=held.instrument_token,
            direction=held.direction,
            entry=held.entry,
            exit=exit_fill,
            gross_pnl=gross,
            costs=costs,
            net_pnl=gross - costs,
            exit_reason=exit_fill.reason,
            ambiguity=ambiguity,
        )
        return PositionBook(), trade

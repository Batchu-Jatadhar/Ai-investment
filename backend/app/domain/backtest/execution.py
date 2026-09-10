"""The execution seam.

Where a strategy's answer stops and the simulator's begins. **Nothing here
fills, prices, or slips anything** - the behaviour arrives in Phase 2.4, and the
configuration it will read (:class:`SlippageConfig`, :class:`ExecutionConfig`)
and the record it will produce (:class:`Fill`) already exist. What was missing
was the thing in between: a statement of what the simulator has been asked to
do.

That statement is :class:`ExecutionIntent`, and its job is to make the approved
model's first rule unrepresentable to break. A signal is produced on the close
of bar N and entered at the open of bar N+1, because nobody can trade on a
price that has already printed. Expressed as a type, "the entry bar starts
strictly after the signal bar" is checked once at construction rather than
being an assumption every future code path has to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.time import ensure_utc
from app.domain.backtest.models import OrderSide
from app.domain.strategy.contract import Signal, SignalDirection

__all__ = ["ExecutionIntent"]


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """An accepted signal, sized, awaiting execution on a named bar.

    ``quantity`` is **supplied, never derived here**. Sizing is the risk
    engine's decision in Phase 3, and in Phase 2 it comes from the fixed
    notional placeholder. This type records the number it was handed so a fill
    can be attributed to it; it has no opinion about what the number should be,
    and no access to the account state that would let it form one.

    ``entry_bar_start`` names the bar the entry executes on - bar N+1 for a
    signal produced on bar N. It is a bar identity rather than a price: what
    that bar opened at is the simulator's to discover, and putting a price here
    would recreate exactly the leak the strategy contract was shaped to prevent.

    Carries no order identifier, no broker reference, no account and no venue.
    A backtest has no orders, only assumptions about how one would have filled,
    and a type that could hold a broker's order id would invite a live-trading
    path to grow through the simulator.
    """

    signal: Signal
    quantity: int
    entry_bar_start: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_bar_start", ensure_utc(self.entry_bar_start))
        if self.quantity <= 0:
            raise ValueError(
                f"quantity must be positive, got {self.quantity}; an intent to trade nothing "
                "is not an intent"
            )
        if self.entry_bar_start <= self.signal.signal_bar_start:
            raise ValueError(
                f"entry_bar_start ({self.entry_bar_start.isoformat()}) must be strictly after "
                f"the signal bar ({self.signal.signal_bar_start.isoformat()}); a signal "
                "produced on a bar's close cannot be filled on that same bar, because that "
                "bar's prices have already printed"
            )

    @property
    def direction(self) -> SignalDirection:
        return self.signal.direction

    @property
    def instrument_token(self) -> int:
        return self.signal.instrument_token

    @property
    def entry_side(self) -> OrderSide:
        """BUY for a long, SELL for a short.

        Derived rather than stored: the side and the direction cannot disagree
        if there is only one of them.
        """
        return OrderSide.entry_for(self.direction)

    @property
    def exit_side(self) -> OrderSide:
        return self.entry_side.opposite

"""The strategy contract - the seam every later phase inserts into.

A strategy is a **pure function of the bars it has already seen**::

    on_bar(session_bars, context) -> Signal | None

``session_bars`` is the completed bars of the current session up to and
including the one that just closed. That is the whole input. A strategy cannot
reach a repository, a clock, a provider, a position or a portfolio, so it can
neither see the future nor carry hidden state between calls. Re-running it over
a prefix of a session must produce exactly what it produced the first time.

What a strategy may emit is deliberately narrow. A :class:`Signal` carries a
direction and two price levels and nothing else:

*   **No quantity.** Sizing is the risk engine's decision (Phase 3).
*   **No entry price.** At the moment a signal is produced nobody knows what the
    next bar will open at; the entry is discovered by the execution simulator.
*   **No order fields, no broker identifiers, no portfolio state.**

This mirrors, one layer down, the architecture's first law about the AI: the
shape of the type is what prevents the mistake, not the discipline of the
caller. A strategy cannot express an entry price, so it cannot invent one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.time import ensure_utc
from app.domain.market.models import Candle, Instrument
from app.domain.market.session import MarketSessionCalendar

__all__ = [
    "Signal",
    "SignalDirection",
    "Strategy",
    "StrategyContext",
]


class SignalDirection(StrEnum):
    """Which way a signal points.

    There is no ``EXIT`` member on purpose: exits belong to the position and its
    stop/target levels, not to the strategy. A strategy proposes entries.
    """

    LONG = "long"
    SHORT = "short"

    @property
    def is_long(self) -> bool:
        return self is SignalDirection.LONG


@dataclass(frozen=True, slots=True)
class Signal:
    """A proposed entry, expressed only as direction and price levels.

    ``stop_price`` is the level at which the setup is structurally invalidated.
    ``target_price`` is where the position would be taken off. Both are absolute
    prices, not distances, so nothing downstream has to guess a reference point.

    The relationship between them is what encodes the direction consistently: a
    long's target is above its stop, a short's below. A signal that violates
    that cannot be constructed.
    """

    instrument_token: int
    direction: SignalDirection
    stop_price: Decimal
    target_price: Decimal
    signal_bar_start: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_bar_start", ensure_utc(self.signal_bar_start))
        if self.instrument_token <= 0:
            raise ValueError(f"instrument_token must be positive, got {self.instrument_token}")
        if not isinstance(self.stop_price, Decimal) or not isinstance(self.target_price, Decimal):
            raise TypeError("stop_price and target_price must be Decimal, never float")
        if self.stop_price <= 0:
            raise ValueError(f"stop_price must be positive, got {self.stop_price}")
        if self.target_price <= 0:
            raise ValueError(f"target_price must be positive, got {self.target_price}")
        if not self.reason.strip():
            raise ValueError("reason must explain why the signal fired; it must not be empty")
        if self.direction.is_long and self.target_price <= self.stop_price:
            raise ValueError(
                f"a long signal's target ({self.target_price}) must be above its "
                f"stop ({self.stop_price})"
            )
        if not self.direction.is_long and self.target_price >= self.stop_price:
            raise ValueError(
                f"a short signal's target ({self.target_price}) must be below its "
                f"stop ({self.stop_price})"
            )


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is legitimately allowed to know at decision time.

    Instrument metadata (tick size, lot size, tradability) and the session's
    boundaries are known in advance and carry no future information: an exchange
    publishes its calendar ahead of time, and tick and lot sizes are properties
    of the contract.

    It deliberately carries **no** repository, clock, provider, position,
    portfolio, open-order state, or any bar the strategy has not been handed. If
    a future field would let a strategy learn something it could not have known
    when that bar closed, it does not belong here.
    """

    instrument: Instrument
    calendar: MarketSessionCalendar
    session_open: datetime
    session_close: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_open", ensure_utc(self.session_open))
        object.__setattr__(self, "session_close", ensure_utc(self.session_close))
        if self.session_open >= self.session_close:
            raise ValueError(
                f"session_open ({self.session_open.isoformat()}) must precede "
                f"session_close ({self.session_close.isoformat()})"
            )


@runtime_checkable
class Strategy(Protocol):
    """A deterministic, stateless producer of signals.

    Implementations are expected to be **pure**: ``on_bar`` must depend only on
    its arguments, and calling it twice with equal arguments must return equal
    results. Nothing here requires a mutable instance, and implementations must
    not use one - any per-session fact a strategy needs (the opening range, or
    whether it has already fired today) is derivable by scanning the bars it was
    given. A session is a few dozen bars, so recomputing is free, and it removes
    the possibility of state leaking across a session boundary or across a run.
    """

    name: str
    version: str

    def on_bar(self, session_bars: Sequence[Candle], context: StrategyContext) -> Signal | None:
        """Decide on the bar that just closed.

        ``session_bars`` holds the current session's completed bars, oldest
        first, ending with the bar being decided on. Returning ``None`` is the
        normal outcome and is never a failure.
        """
        ...

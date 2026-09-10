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

That is also why the target is an **R multiple rather than a price**. R is the
distance from entry to stop, and the entry is not known until the fill, so a
strategy that had to name an absolute target price would have to assume one -
and the assumed entry would then be recoverable from stop and target together,
which is the leak this contract exists to prevent. The hypothesis says "target:
2R"; the signal says ``2`` and the execution simulator turns it into a price
once it knows what was actually paid.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.time import ensure_utc, to_ist
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

    ``stop_price`` is an absolute price: the level at which the setup is
    structurally invalidated. A strategy genuinely knows it - for the opening
    range breakout it is the far side of the opening range, which is a fact
    about bars that have already closed.

    ``target_r_multiple`` is a multiple of R, not a price. R is the distance
    from entry to stop, and the entry is not known until the fill, so this is
    the only honest way to state a target at signal time. The execution
    simulator resolves it against the actual entry.

    ``signal_bar_start`` identifies the bar that was being decided on, which is
    what lets a signal be traced back to the exact input that produced it.
    """

    instrument_token: int
    direction: SignalDirection
    stop_price: Decimal
    target_r_multiple: Decimal
    signal_bar_start: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_bar_start", ensure_utc(self.signal_bar_start))
        if self.instrument_token <= 0:
            raise ValueError(f"instrument_token must be positive, got {self.instrument_token}")
        for name in ("stop_price", "target_r_multiple"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        if self.stop_price <= 0:
            raise ValueError(f"stop_price must be positive, got {self.stop_price}")
        if self.target_r_multiple <= 0:
            raise ValueError(
                f"target_r_multiple must be positive, got {self.target_r_multiple}; a target at "
                "or behind the entry is not a target"
            )
        if not self.reason.strip():
            raise ValueError("reason must explain why the signal fired; it must not be empty")
        # The remaining invariant - a long stops below its entry, a short above -
        # cannot be checked here, because it relates the stop to an entry price
        # that does not exist yet. It is checked at fill time, where both numbers
        # are known, rather than guessed at here against an assumed entry.


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is legitimately allowed to know at decision time.

    Instrument metadata (tick size, lot size, tradability) and the session's
    boundaries are known in advance and carry no future information: an exchange
    publishes its calendar ahead of time, and tick and lot sizes are properties
    of the contract.

    ``prior_atr`` is the one fact here that comes from bars rather than from a
    published schedule, and it is the reason this field exists at all: it is
    computed from sessions **strictly before** this one, so it is knowable at
    the opening bell and yet unreachable from ``session_bars``, which holds only
    today. Handing the strategy earlier sessions instead would have widened the
    window it can see, and lookahead is prevented here precisely by keeping that
    window one session wide. It is ``None`` when there is not enough history to
    form an ATR; a strategy that needs it must then decline to trade rather than
    substitute a value.

    It deliberately carries **no** repository, clock, provider, position,
    portfolio, open-order state, or any bar the strategy has not been handed. If
    a future field would let a strategy learn something it could not have known
    when that bar closed, it does not belong here.
    """

    instrument: Instrument
    calendar: MarketSessionCalendar
    session_open: datetime
    session_close: datetime
    prior_atr: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_open", ensure_utc(self.session_open))
        object.__setattr__(self, "session_close", ensure_utc(self.session_close))
        if self.session_open >= self.session_close:
            raise ValueError(
                f"session_open ({self.session_open.isoformat()}) must precede "
                f"session_close ({self.session_close.isoformat()})"
            )
        if self.prior_atr is not None:
            if not isinstance(self.prior_atr, Decimal):
                raise TypeError("prior_atr must be Decimal, never float")
            if self.prior_atr <= 0:
                raise ValueError(
                    f"prior_atr must be positive, got {self.prior_atr}; an instrument that did "
                    "not move at all over the lookback is missing data, not a zero-volatility "
                    "instrument - pass None instead"
                )

    @property
    def session_date(self) -> date:
        """The IST calendar date of this session.

        Derived rather than stored, so it cannot drift out of step with
        ``session_open``. This is what the opening-range calculation is keyed
        on, and IST is the zone session boundaries are defined in.
        """
        return to_ist(self.session_open).date()


@runtime_checkable
class Strategy(Protocol):
    """A deterministic, stateless producer of signals.

    Implementations are expected to be **pure**: ``on_bar`` must depend only on
    its arguments and on immutable configuration fixed at construction, and
    calling it twice with equal arguments must return equal results.

    Immutable configuration is the one thing an instance may hold. A strategy
    constructed with a frozen ``OrbParams`` is still a pure function - the same
    params and the same bars always give the same signal - and that is how
    parameters reach a strategy, rather than through the context, which would
    make one shared contract carry one strategy's settings.

    **Mutable state is forbidden.** Any per-session fact a strategy needs (the
    opening range, or whether it has already fired today) is derivable by
    scanning the bars it was given. A session is a few dozen bars, so
    recomputing is free, and it removes the possibility of state leaking across
    a session boundary or across a run - which would make a result depend on the
    sequence sessions happened to be replayed in.

    ``name`` and ``version`` are recorded in the run manifest, so a stored
    result can be traced back to the code that produced it. Bump ``version``
    whenever the signal logic changes.
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

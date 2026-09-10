"""Opening Range Breakout - breakout detection.

One question, answered purely: has this completed 5-minute bar closed beyond
the opening range, and if so which way?

Three rules from the approved hypothesis, and each is one comparison rather
than a special case, which is why they cannot drift apart:

*   **The close decides, not the wick.** A bar that pokes through the range and
    comes back has been rejected at that level, and treating it as a breakout is
    how a strategy ends up buying the high of the day. Reading ``close`` and
    never ``high`` or ``low`` is what encodes that.
*   **An exact touch is not a breakout.** The comparisons are strict, so a close
    landing precisely on the boundary is inside the range. The range levels are
    where everyone else's stops sit; trading a bar that merely reached one is
    trading noise.
*   **Only completed bars decide.** An in-progress bar's close still moves, so a
    decision taken on one is a decision taken on a number that had not settled.
    That is rejected rather than ignored - silently skipping it would look
    exactly like "no breakout" and hide a wiring error.

Detection is kept separate from the strategy that uses it, so each can be
tested against hand-built bars where the answer is known in advance.

.. rubric:: The strategy

:class:`OrbStrategy` turns a detected breakout into a :class:`Signal`. It holds
its parameters and nothing else - no mutable state, no clock, no history beyond
the session prefix it is handed - so the same bars always produce the same
signal, whatever order sessions were replayed in.

Every evaluation yields an :class:`OrbDecision`: the signal if one fired, and in
every case a typed :class:`OrbReason` saying why. Recording why a bar did *not*
signal is what makes a run explainable - "no trades today" and "every setup was
rejected as too wide" look identical in an equity curve and are completely
different facts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import ClassVar

from app.core.time import to_ist
from app.domain.indicators import OpeningRange, opening_range
from app.domain.market.models import Candle, CandleStatus
from app.domain.strategy.contract import Signal, SignalDirection, StrategyContext
from app.domain.strategy.params import OrbParams

__all__ = [
    "OrbDecision",
    "OrbReason",
    "OrbStrategy",
    "breakout_direction",
]


def breakout_direction(candle: Candle, opening_range: OpeningRange) -> SignalDirection | None:
    """Which way ``candle`` broke ``opening_range``, or ``None`` for neither.

    ``None`` is the normal outcome and never signifies a failure: most bars in
    a session close inside the range, and a bar belonging to the range itself
    cannot break a range it is still forming.

    Raises ``ValueError`` if the bar has not completed.
    """
    if candle.status is not CandleStatus.COMPLETED:
        raise ValueError(
            f"the bar at {candle.start_at.isoformat()} is {candle.status.value}; a breakout is "
            "decided on a completed bar, because an in-progress bar's close still moves and the "
            "decision would rest on a number that had not settled"
        )

    if candle.start_at < opening_range.end_at:
        # A bar inside the opening window is one of the bars forming the range.
        # Comparing it against a range it is still building would let the first
        # bar of the session "break out" of itself.
        return None

    if candle.close > opening_range.high:
        return SignalDirection.LONG
    if candle.close < opening_range.low:
        return SignalDirection.SHORT
    return None


class OrbReason(StrEnum):
    """Why a bar did or did not produce a signal.

    Typed rather than free text because these are compared, counted and
    asserted on. A rejection tally is one of the first things worth looking at
    when a run produces fewer trades than expected, and string literals drift
    apart the moment two of them mean the same thing.

    The values are the strings that reach :attr:`Signal.reason` and the signal
    log, so they are written in the log's idiom rather than Python's.
    """

    LONG_BREAKOUT = "LONG_ORB_BREAKOUT"
    SHORT_BREAKOUT = "SHORT_ORB_BREAKOUT"
    OPENING_RANGE_INCOMPLETE = "OPENING_RANGE_INCOMPLETE"
    NO_BREAKOUT = "NO_BREAKOUT"
    RANGE_TOO_NARROW = "RANGE_TOO_NARROW"
    RANGE_TOO_WIDE = "RANGE_TOO_WIDE"
    ATR_UNAVAILABLE = "ATR_UNAVAILABLE"
    ENTRY_CUTOFF_REACHED = "ENTRY_CUTOFF_REACHED"
    DIRECTION_ALREADY_SIGNALLED = "DIRECTION_ALREADY_SIGNALLED"

    @classmethod
    def for_direction(cls, direction: SignalDirection) -> OrbReason:
        return cls.LONG_BREAKOUT if direction.is_long else cls.SHORT_BREAKOUT


@dataclass(frozen=True, slots=True)
class OrbDecision:
    """What the strategy concluded about one bar, and why.

    ``signal`` is ``None`` for every reason other than a breakout. The pair
    travels together so a caller cannot log the outcome without the explanation
    - which is exactly what happens when the reason is returned separately and
    the ``None`` path is the one nobody instruments.
    """

    signal: Signal | None
    reason: OrbReason

    def __post_init__(self) -> None:
        fired = self.reason in (OrbReason.LONG_BREAKOUT, OrbReason.SHORT_BREAKOUT)
        if fired and self.signal is None:
            raise ValueError(f"reason {self.reason.value} claims a breakout but carries no signal")
        if not fired and self.signal is not None:
            raise ValueError(
                f"reason {self.reason.value} is a rejection but carries a signal; a rejected "
                "setup must not leave a tradable signal behind"
            )


@dataclass(frozen=True, slots=True)
class OrbStrategy:
    """Opening Range Breakout, as a pure function of the session so far.

    The instance holds ``params`` and nothing else. Frozen configuration is not
    state: the same params and the same bars always give the same signal, which
    is what lets a result be reproduced from its manifest alone.

    A long stops at the opening range low and a short at its high - the level
    that would prove the setup wrong, which is a fact about bars that have
    already closed. The target is ``target_r_multiple`` from the hypothesis,
    left as a multiple because the entry it is measured from does not exist
    until the fill.
    """

    params: OrbParams = field(default_factory=OrbParams)

    name: ClassVar[str] = "orb"
    version: ClassVar[str] = "1"

    def on_bar(self, session_bars: Sequence[Candle], context: StrategyContext) -> Signal | None:
        """The contract entry point: the signal, if one fired."""
        return self.evaluate(session_bars, context).signal

    def evaluate(self, session_bars: Sequence[Candle], context: StrategyContext) -> OrbDecision:
        """Decide on the last bar of ``session_bars``, with the reason attached.

        ``session_bars`` is the current session's completed bars, oldest first,
        ending with the bar being decided on.

        A session whose opening window is genuinely incomplete - a bar missing
        inside 09:15-09:30, or a date the calendar says is not a trading day -
        raises rather than returning a rejection. That is missing data, not a
        setup the strategy declined, and quietly reporting it as the latter
        would make a gap in the feed indistinguishable from a quiet morning.
        """
        if not session_bars:
            raise ValueError(
                "session_bars is empty; on_bar decides on the bar that just closed, so there "
                "must be at least one"
            )

        current = session_bars[-1]
        window_end = context.session_open + timedelta(minutes=self.params.opening_range_minutes)
        if current.start_at < window_end:
            # Still inside the opening window: these bars are forming the range,
            # and there is nothing yet to break out of.
            return OrbDecision(None, OrbReason.OPENING_RANGE_INCOMPLETE)

        measured = opening_range(
            session_bars,
            context.session_date,
            context.calendar,
            opening_range_minutes=self.params.opening_range_minutes,
        )

        direction = breakout_direction(current, measured)
        if direction is None:
            return OrbDecision(None, OrbReason.NO_BREAKOUT)

        if self._already_signalled(session_bars, measured, direction, context):
            return OrbDecision(None, OrbReason.DIRECTION_ALREADY_SIGNALLED)

        rejection = self._rejection(current, measured, context)
        if rejection is not None:
            return OrbDecision(None, rejection)

        return OrbDecision(
            self._signal(current, measured, direction, context),
            OrbReason.for_direction(direction),
        )

    def _already_signalled(
        self,
        session_bars: Sequence[Candle],
        measured: OpeningRange,
        direction: SignalDirection,
        context: StrategyContext,
    ) -> bool:
        """Has an earlier bar of this session already fired in ``direction``?

        **Derived from the prefix, never remembered.** A ``has_signalled_long``
        flag on the instance would be the obvious implementation and is exactly
        what makes a run irreproducible: the answer would depend on which
        sessions had been replayed through this object beforehand, so replaying
        one session alone would give a different result than replaying it inside
        a year. Re-deriving costs a rescan and buys the guarantee that a session
        decides its own outcome.

        Session reset falls out of this for free. A new session arrives as a new
        prefix containing none of yesterday's bars, so there is nothing to
        forget and no boundary at which forgetting could be missed.

        A rejected setup does not consume the direction. It never became a
        signal, so the day has not had its trade, which is why the rejection
        check is repeated here rather than assumed.

        ponytail: rescans the prefix per bar, so a session costs O(n^2) - about
        2,800 comparisons for a 75-bar day, which is free. Memoise per session
        if an interval far finer than 1m ever makes it matter.
        """
        for earlier in session_bars[:-1]:
            if breakout_direction(earlier, measured) is not direction:
                continue
            if self._rejection(earlier, measured, context) is None:
                return True
        return False

    def _rejection(
        self, candle: Candle, measured: OpeningRange, context: StrategyContext
    ) -> OrbReason | None:
        """Why this breakout must not be traded, or ``None`` to take it.

        Checked only once a breakout has actually fired. Filtering earlier would
        stamp a rejection on every quiet bar of every unusable day, and a log
        where most entries are noise is a log nobody reads. Asked in this order
        the reason is the most specific one true of the setup: the two range
        checks describe the whole session and invalidate it from 09:30 onwards,
        so they outrank the cutoff, which only says this particular bar came
        too late.
        """
        params = self.params

        minimum = params.min_range_ticks * context.instrument.tick_size
        if measured.width < minimum:
            # A range this tight puts the stop inside the spread, where the
            # exit is decided by the book rather than by the setup failing.
            return OrbReason.RANGE_TOO_NARROW

        if context.prior_atr is None:
            # The ceiling cannot be evaluated, so the setup cannot be cleared.
            # Declining is the contract: substituting a value would silently
            # trade the days the filter exists to skip.
            return OrbReason.ATR_UNAVAILABLE

        if measured.width > params.max_range_atr_multiple * context.prior_atr:
            # A range this wide relative to normal movement puts a 2R target
            # further away than the instrument usually travels in a day.
            return OrbReason.RANGE_TOO_WIDE

        if to_ist(candle.end_at).time() > params.no_new_entry_after:
            # The cutoff is about the *entry*, which happens on the bar after
            # this one - so it is this bar's close, not its open, that has to
            # land at or before the cutoff.
            return OrbReason.ENTRY_CUTOFF_REACHED

        return None

    def _signal(
        self,
        candle: Candle,
        measured: OpeningRange,
        direction: SignalDirection,
        context: StrategyContext,
    ) -> Signal:
        return Signal(
            instrument_token=context.instrument.instrument_token,
            direction=direction,
            stop_price=measured.low if direction.is_long else measured.high,
            target_r_multiple=self.params.target_r_multiple,
            signal_bar_start=candle.start_at,
            reason=OrbReason.for_direction(direction),
        )

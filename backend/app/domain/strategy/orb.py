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

This is deliberately only the detection. Whether a detected breakout is worth
trading - the minimum range, the ATR ceiling, the entry cutoff - and what stop
and target it implies are separate decisions, and they arrive with the strategy
itself. Keeping them apart is what lets each be tested against hand-built bars
where the answer is known in advance.
"""

from __future__ import annotations

from app.domain.indicators import OpeningRange
from app.domain.market.models import Candle, CandleStatus
from app.domain.strategy.contract import SignalDirection

__all__ = ["breakout_direction"]


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

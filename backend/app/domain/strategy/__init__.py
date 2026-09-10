"""Deterministic strategy contract and the first strategy.

Implemented in Phase 2.0:
  * the strategy contract - ``on_bar(session_bars, context) -> Signal | None``
  * ``Signal``, carrying a direction and price levels and nothing else
  * ``StrategyContext``, holding only what is knowable at decision time
  * ``OrbParams``, the Opening Range Breakout INITIAL FIXED HYPOTHESIS

Refined in Phase 2.2 so the ORB can be expressed without bending the contract:
a signal states its target as an R multiple rather than a price, since the entry
it would be measured from does not exist yet, and the context carries the prior
sessions' ATR, which is knowable at the opening bell but unreachable from bars
the strategy is handed.

Still to come:
  * the Opening Range Breakout strategy itself
  * signal deduplication, conflict resolution and ranking, if a second strategy
    ever makes them necessary

A strategy is a pure function of the bars it has been handed. It imports no
broker, no order code and no AI, and the architecture-purity tests enforce that.
"""

from app.domain.strategy.contract import (
    Signal,
    SignalDirection,
    Strategy,
    StrategyContext,
)
from app.domain.strategy.params import OrbParams

__all__ = [
    "OrbParams",
    "Signal",
    "SignalDirection",
    "Strategy",
    "StrategyContext",
]

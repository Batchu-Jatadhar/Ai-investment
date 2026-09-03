"""Deterministic strategy contract and the first strategy.

Implemented in Phase 2.0:
  * the strategy contract - ``on_bar(session_bars, context) -> Signal | None``
  * ``Signal``, carrying a direction and price levels and nothing else
  * ``StrategyContext``, holding only what is knowable at decision time
  * ``OrbParams``, the Opening Range Breakout INITIAL FIXED HYPOTHESIS

Still to come:
  * ATR and opening-range features (Phase 2.1)
  * the Opening Range Breakout strategy itself (Phase 2.2)
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

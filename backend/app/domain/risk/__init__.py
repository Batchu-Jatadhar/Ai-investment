"""Deterministic risk engine. PASS/REJECT with machine-readable reasons.

Implemented: ``sizing`` - risk-budget position sizing with structured rejections.
It is invoked explicitly; the Phase 2 backtest engine still sizes by fixed notional.

Planned responsibilities:
  * per-trade, daily, portfolio, instrument, session, market gates
  * a REJECT is absolute and has no override path
"""

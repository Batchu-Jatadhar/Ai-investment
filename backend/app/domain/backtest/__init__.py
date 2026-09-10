"""Offline backtesting.

Phase 2.0 implements the value objects only: the immutable
:class:`~app.domain.backtest.input.BacktestInput`, the configuration objects
that record a run's assumptions, and the result models.

Nothing in this package simulates anything yet. Phase 2.3 adds
:class:`~app.domain.backtest.execution.ExecutionIntent`, the seam that states
what the simulator has been asked to do; the simulator that acts on it arrives
in Phase 2.4, along with portfolio accounting, then metrics in Phase 2.5 and the
engine that sequences them in Phase 2.6.

It lives under ``app/domain`` because it is pure: it imports no adapter, no
broker, no ORM and no clock, and the architecture-purity tests enforce that.
``app/domain/execution`` is reserved for the *live* supervisor and must not be
conflated with this simulation.
"""

from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.execution import (
    ExecutionIntent,
    resolve_stop_fill,
    resolve_target_fill,
)
from app.domain.backtest.input import BacktestInput, InvalidBacktestInputError
from app.domain.backtest.models import (
    AmbiguityResolution,
    BacktestResult,
    EquityPoint,
    Fill,
    FillReason,
    OrderSide,
    RunManifest,
    SignalRecord,
    Trade,
)

__all__ = [
    "AmbiguityResolution",
    "BacktestInput",
    "BacktestResult",
    "CostSchedule",
    "EquityPoint",
    "ExecutionConfig",
    "ExecutionIntent",
    "Fill",
    "FillReason",
    "InvalidBacktestInputError",
    "OrderSide",
    "RunManifest",
    "SignalRecord",
    "SlippageConfig",
    "Trade",
    "resolve_stop_fill",
    "resolve_target_fill",
]

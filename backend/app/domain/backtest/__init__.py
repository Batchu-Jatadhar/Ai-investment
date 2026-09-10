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

from app.domain.backtest.cash import CashLedger
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.execution import (
    EntryOutcome,
    ExecutionIntent,
    ExecutionStatus,
    ExitResolution,
    UnexecutableBarError,
    resolve_entry_fill,
    resolve_exit_fill,
    resolve_hard_exit_fill,
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
from app.domain.backtest.position import Position, PositionBook, PositionTransitionError
from app.domain.backtest.sizing import PositionSizeError, fixed_notional_quantity

__all__ = [
    "AmbiguityResolution",
    "BacktestInput",
    "BacktestResult",
    "CashLedger",
    "CostSchedule",
    "EntryOutcome",
    "EquityPoint",
    "ExecutionConfig",
    "ExecutionIntent",
    "ExecutionStatus",
    "ExitResolution",
    "Fill",
    "FillReason",
    "InvalidBacktestInputError",
    "OrderSide",
    "Position",
    "PositionBook",
    "PositionSizeError",
    "PositionTransitionError",
    "RunManifest",
    "SignalRecord",
    "SlippageConfig",
    "Trade",
    "UnexecutableBarError",
    "fixed_notional_quantity",
    "resolve_entry_fill",
    "resolve_exit_fill",
    "resolve_hard_exit_fill",
    "resolve_stop_fill",
    "resolve_target_fill",
]

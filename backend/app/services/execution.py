"""The execution-mode boundary: the one place a trading mode becomes an executor.

PAPER gets the paper adapter, which has no broker to call. Every other mode
fails closed: LIVE because live execution is not implemented, BACKTEST because
the backtest engine drives the fill resolvers itself and has no use for a port.
There is deliberately no branch that could return a broker adapter.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from app.adapters.paper import PaperExecutionAdapter
from app.config.settings import TradingMode
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.execution.ports import ExecutionPort
from app.domain.market.models import CandleInterval, Instrument

__all__ = ["ExecutionModeError", "build_execution_port"]


class ExecutionModeError(RuntimeError):
    """Raised when a trading mode has no permitted execution path."""


def build_execution_port(
    mode: TradingMode,
    *,
    instrument: Instrument,
    starting_capital: Decimal,
    signal_interval: CandleInterval,
    hard_exit_time: time,
    execution: ExecutionConfig,
    slippage: SlippageConfig,
    cost_schedule: CostSchedule,
) -> ExecutionPort:
    if mode is not TradingMode.PAPER:
        raise ExecutionModeError(
            f"trading mode {mode.value!r} has no execution port in this build; only paper "
            "execution exists, and it never reaches a broker"
        )
    return PaperExecutionAdapter(
        instrument=instrument,
        starting_capital=starting_capital,
        signal_interval=signal_interval,
        hard_exit_time=hard_exit_time,
        execution=execution,
        slippage=slippage,
        cost_schedule=cost_schedule,
    )

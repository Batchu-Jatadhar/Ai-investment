"""The execution port: what the rest of the system may ask an executor to do.

Broker-neutral. It names no vendor, carries no credential, and has no notion of
a network. The one implementation today is the paper adapter, which simulates
fills from completed bars; a live implementation does not exist.

.. rubric:: Order lifecycle

::

    submit --> ACCEPTED --bar--> FILLED
           \           \--cancel--> CANCELLED
            \           \--entry bar missing--> EXPIRED
             \--> REJECTED

Every arrow not drawn raises :class:`InvalidTransitionError`. A business refusal
at submit (a rejected risk decision, an order already working) is a
``REJECTED`` record with its reasons, not an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.domain.backtest.models import Fill, Trade
from app.domain.backtest.position import Position
from app.domain.risk.sizing import RiskDecision
from app.domain.strategy.contract import Signal

__all__ = [
    "ExecutionPort",
    "InvalidTransitionError",
    "OrderRecord",
    "OrderStatus",
]


class InvalidTransitionError(ValueError):
    """An operation the order or position lifecycle does not have."""


class OrderStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self is not OrderStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """One entry order and what became of it."""

    client_order_id: str
    signal: Signal
    quantity: int
    status: OrderStatus
    reasons: tuple[str, ...] = ()
    fill: Fill | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id must identify the order")
        if (self.status is OrderStatus.REJECTED) != bool(self.reasons):
            raise ValueError("a rejected order needs reasons, and only a rejected order has them")
        if (self.status is OrderStatus.FILLED) != (self.fill is not None):
            raise ValueError("a filled order carries its fill, and only a filled order does")


class ExecutionPort(Protocol):
    """Submit, cancel and flatten. Fills arrive from the executor, never from the caller."""

    def submit(
        self, client_order_id: str, signal: Signal, decision: RiskDecision
    ) -> OrderRecord: ...

    def cancel(self, client_order_id: str) -> OrderRecord: ...

    def flatten(self) -> None: ...

    def order(self, client_order_id: str) -> OrderRecord: ...

    @property
    def position(self) -> Position | None: ...

    @property
    def trades(self) -> tuple[Trade, ...]: ...

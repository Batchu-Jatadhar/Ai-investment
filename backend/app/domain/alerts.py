"""External alerts: advisory events that arrive from outside the system.

A charting or monitoring tool can notice something - a level broken, a pattern
formed - and announce it. This module is the domain's vendor-neutral view of
that announcement. It knows no provider; adapters translate their own payloads
into :class:`ExternalAlert`.

.. rubric:: An alert is information, never an instruction

:class:`ExternalAlert` deliberately carries **no quantity, entry price, stop,
target, order type or product**. Those are decisions for the strategy and risk
layers, made from this system's own market data. The shape of the type is what
keeps an outside tool from sizing or pricing a trade: it has nowhere to put the
numbers. And an alert is not market data - candles and ticks still come only
from the market-data pipeline.

Nothing in the application consumes these alerts for execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.time import ensure_utc

__all__ = ["AlertAction", "AlertLedger", "ExternalAlert"]


class AlertAction(StrEnum):
    """What the alert says happened. A direction of interest, not an order side."""

    LONG = "long"
    SHORT = "short"
    EXIT = "exit"
    NOTIFY = "notify"


@dataclass(frozen=True, slots=True)
class ExternalAlert:
    """One accepted, authenticated, de-duplicated external alert."""

    #: Which adapter produced it, e.g. the name of the integration.
    source: str
    #: The sender's own identifier for the event; unique per source.
    event_id: str
    exchange: str
    tradingsymbol: str
    action: AlertAction
    #: When the sender says the event happened.
    occurred_at: datetime
    #: When this system accepted it.
    received_at: datetime
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        for name in ("source", "event_id", "exchange", "tradingsymbol"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")


@runtime_checkable
class AlertLedger(Protocol):
    """Remembers which alerts have been accepted, so none is accepted twice."""

    def record_if_new(self, alert: ExternalAlert) -> bool:
        """Record ``alert`` and return ``True``, or ``False`` if its
        ``(source, event_id)`` was already recorded. Atomic: two concurrent
        deliveries of the same event cannot both return ``True``."""
        ...

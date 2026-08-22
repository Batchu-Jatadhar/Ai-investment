"""Deterministic replay market-data provider.

**This is not live data and must never be presented as such.** The name says so,
``name`` reports ``"replay"``, and every tick and candle it produces carries
``source="replay"``. There is no code path that lets it masquerade as the
Zerodha provider.

Its purpose is deterministic tests and, later, replaying a recorded session
through the same pipeline the live feed uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime, timedelta

from app.core.time import Clock, SystemClock
from app.domain.market.models import MarketTick, TickMode
from app.domain.market.ports import (
    ConnectionEvent,
    ConnectionEventType,
    ProviderState,
    StreamEvent,
    TickBatch,
)

__all__ = ["FakeMarketDataProvider", "ReplayMarketDataProvider"]


class ReplayMarketDataProvider:
    """Replays a fixed sequence of events. Deterministic, offline, never live."""

    name = "replay"

    def __init__(
        self,
        events: Sequence[StreamEvent] | None = None,
        *,
        clock: Clock | None = None,
        emit_lifecycle: bool = True,
    ) -> None:
        self._events: list[StreamEvent] = list(events or [])
        self._clock = clock or SystemClock()
        self._emit_lifecycle = emit_lifecycle
        self._state = ProviderState.NOT_CONFIGURED
        self._tokens: set[int] = set()
        self._modes: dict[int, TickMode] = {}
        self.subscribe_calls: list[tuple[tuple[int, ...], TickMode]] = []
        self.unsubscribe_calls: list[tuple[int, ...]] = []

    # ------------------------------------------------------------------ #

    @classmethod
    def from_ticks(
        cls,
        ticks: Iterable[MarketTick],
        *,
        clock: Clock | None = None,
        batch_size: int = 1,
    ) -> ReplayMarketDataProvider:
        """Build a provider that emits ``ticks`` in batches, in order."""
        pending = list(ticks)
        events: list[StreamEvent] = []
        for start in range(0, len(pending), max(1, batch_size)):
            chunk = tuple(pending[start : start + max(1, batch_size)])
            events.append(
                TickBatch(ticks=chunk, received_at=chunk[0].received_at, provider=cls.name)
            )
        return cls(events, clock=clock)

    def add(self, event: StreamEvent) -> None:
        self._events.append(event)

    # ------------------------------------------------------------------ #

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def labelled_state(self) -> str:
        return self._state.labelled(self.name)

    @property
    def subscribed_tokens(self) -> frozenset[int]:
        return frozenset(self._tokens)

    @property
    def is_configured(self) -> bool:
        return True

    async def connect(self) -> None:
        self._state = ProviderState.CONNECTED

    async def disconnect(self) -> None:
        self._state = ProviderState.STOPPED

    async def subscribe(self, tokens: Iterable[int], mode: TickMode = TickMode.FULL) -> None:
        requested = tuple(sorted({int(t) for t in tokens}))
        self.subscribe_calls.append((requested, mode))
        self._tokens |= set(requested)
        for token in requested:
            self._modes[token] = mode

    async def unsubscribe(self, tokens: Iterable[int]) -> None:
        requested = tuple(sorted({int(t) for t in tokens} & self._tokens))
        self.unsubscribe_calls.append(requested)
        self._tokens -= set(requested)
        for token in requested:
            self._modes.pop(token, None)

    async def stream(self) -> AsyncIterator[StreamEvent]:
        if self._emit_lifecycle:
            self._state = ProviderState.AUTHENTICATED
            yield ConnectionEvent(
                event_type=ConnectionEventType.AUTH_SUCCEEDED,
                provider=self.name,
                occurred_at=self._clock.now(),
                state=self._state,
                detail={"replay": True},
            )
            self._state = ProviderState.CONNECTED
            yield ConnectionEvent(
                event_type=ConnectionEventType.CONNECTED,
                provider=self.name,
                occurred_at=self._clock.now(),
                state=self._state,
                detail={"replay": True},
            )

        for event in self._events:
            yield event

        self._state = ProviderState.STOPPED
        if self._emit_lifecycle:
            yield ConnectionEvent(
                event_type=ConnectionEventType.DISCONNECTED,
                provider=self.name,
                occurred_at=self._clock.now(),
                state=self._state,
                detail={"reason": "replay_exhausted"},
            )

    def health(self, now: datetime | None = None) -> dict[str, object]:
        return {
            "provider": self.name,
            "state": self.labelled_state,
            "configured": True,
            "authenticated": self._state is not ProviderState.NOT_CONFIGURED,
            "connected": self._state is ProviderState.CONNECTED,
            "subscribed_instruments": len(self._tokens),
            "replay": True,
            "pending_events": len(self._events),
        }


#: Alias for tests that want the intent spelled out at the call site.
FakeMarketDataProvider = ReplayMarketDataProvider


def spaced_ticks(
    template: MarketTick, count: int, step: timedelta
) -> list[MarketTick]:  # pragma: no cover - fixture helper
    """Produce ``count`` ticks spaced by ``step`` from a template tick."""
    from dataclasses import replace

    out: list[MarketTick] = []
    for index in range(count):
        shift = step * index
        out.append(
            replace(
                template,
                received_at=template.received_at + shift,
                exchange_timestamp=(
                    template.exchange_timestamp + shift if template.exchange_timestamp else None
                ),
            )
        )
    return out

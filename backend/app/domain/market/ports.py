"""Ports the market-data domain depends on.

The domain depends on these protocols and never on a concrete broker. Swapping
Zerodha for another provider, or for the replay provider, means supplying a
different implementation of :class:`MarketDataProvider` - no domain change.

    Strategy / services
            |
            v
    MarketDataProvider   MarketDataRepository   InstrumentSource   (this module)
            |                     |                    |
            v                     v                    v
    ZerodhaMarketDataProvider   SqlMarketData...   ZerodhaRestClient
    ReplayMarketDataProvider                       (adapters)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.domain.market.models import (
    Candle,
    CandleInterval,
    Instrument,
    MarketTick,
    TickMode,
)
from app.domain.market.quality import DataQualityEvent

__all__ = [
    "ConnectionEvent",
    "ConnectionEventType",
    "DataGap",
    "InstrumentSource",
    "MarketDataProvider",
    "MarketDataRepository",
    "ProviderState",
    "StreamEvent",
    "TickBatch",
]


class ProviderState(StrEnum):
    """Lifecycle of a market-data provider.

    Rendered in health output prefixed by the provider name, e.g.
    ``ZERODHA_CONNECTED``. A provider must never report data while in a state
    other than ``CONNECTED``.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTHENTICATED = "AUTHENTICATED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    STOPPED = "STOPPED"

    @property
    def is_live(self) -> bool:
        return self is ProviderState.CONNECTED

    def labelled(self, provider: str) -> str:
        return f"{provider.upper()}_{self.value}"


class ConnectionEventType(StrEnum):
    CONFIGURED = "configured"
    NOT_CONFIGURED = "not_configured"
    AUTH_SUCCEEDED = "auth_succeeded"
    AUTH_FAILED = "auth_failed"
    CONNECT_ATTEMPT = "connect_attempt"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTED = "reconnected"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    RESUBSCRIBED = "resubscribed"
    MODE_CHANGED = "mode_changed"
    DATA_GAP = "data_gap"
    PROVIDER_MESSAGE = "provider_message"
    PROVIDER_ERROR = "provider_error"
    STREAM_STALE = "stream_stale"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """A recorded lifecycle event. Persisted, so gaps stay visible after the fact."""

    event_type: ConnectionEventType
    provider: str
    occurred_at: datetime
    state: ProviderState | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DataGap:
    """A period during which the stream was not delivering.

    Recorded explicitly. A reconnect is never treated as though no data was
    missed - the gap is a fact about the data, and any bar overlapping it is
    suspect.
    """

    provider: str
    started_at: datetime
    ended_at: datetime
    reason: str
    instrument_tokens: tuple[int, ...] = ()

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class TickBatch:
    """Ticks decoded from one transport frame, kept together for accounting."""

    ticks: tuple[MarketTick, ...]
    received_at: datetime
    provider: str


StreamEvent = TickBatch | ConnectionEvent | DataGap


@runtime_checkable
class MarketDataProvider(Protocol):
    """A live or replayed source of market data."""

    name: str

    @property
    def state(self) -> ProviderState: ...

    @property
    def subscribed_tokens(self) -> frozenset[int]: ...

    async def connect(self) -> None:
        """Authenticate if required and establish the stream."""
        ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, tokens: Iterable[int], mode: TickMode = TickMode.FULL) -> None:
        """Subscribe. Re-subscribing to an already-subscribed token is a no-op."""
        ...

    async def unsubscribe(self, tokens: Iterable[int]) -> None: ...

    def stream(self) -> AsyncIterator[StreamEvent]:
        """Yield ticks and lifecycle events until the provider is stopped."""
        ...


@runtime_checkable
class InstrumentSource(Protocol):
    """Somewhere the instrument dump can be fetched from."""

    name: str

    async def fetch_instruments(self, exchange: str | None = None) -> list[Instrument]: ...


@runtime_checkable
class MarketDataRepository(Protocol):
    """Persistence and query surface for market data.

    Downstream modules ask this, not the provider. Whether an answer comes from
    the live stream, the database or a replay source is not their concern.

    Every method a domain or service module calls must be declared here. A
    caller reaching past the port for an implementation-specific method is the
    seam quietly failing: a substitute repository that satisfies this protocol
    would then break at runtime.
    """

    # -- instruments ----------------------------------------------------
    def replace_instruments(
        self, instruments: Sequence[Instrument], retrieved_at: datetime
    ) -> int: ...

    def get_instrument_by_token(self, instrument_token: int) -> Instrument | None: ...

    def get_instrument_by_symbol(self, exchange: str, tradingsymbol: str) -> Instrument | None: ...

    def all_instruments(self, limit: int = 100_000) -> list[Instrument]:
        """Every stored instrument, for populating an in-memory lookup cache."""
        ...

    def instrument_count(self) -> int: ...

    def instruments_retrieved_at(self) -> datetime | None: ...

    # -- ticks ----------------------------------------------------------
    def save_ticks(self, ticks: Sequence[MarketTick]) -> int: ...

    def latest_tick(self, instrument_token: int) -> MarketTick | None: ...

    def ticks_in_range(
        self, instrument_token: int, start: datetime, end: datetime, limit: int = 10_000
    ) -> list[MarketTick]: ...

    # -- candles --------------------------------------------------------
    def save_candles(self, candles: Sequence[Candle]) -> int: ...

    def latest_completed_candle(
        self, instrument_token: int, interval: CandleInterval
    ) -> Candle | None: ...

    def candles_in_range(
        self,
        instrument_token: int,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
        limit: int = 5_000,
    ) -> list[Candle]: ...

    def recent_candles(
        self, instrument_token: int, interval: CandleInterval, count: int
    ) -> list[Candle]: ...

    # -- operational events ---------------------------------------------
    def record_connection_event(self, event: ConnectionEvent) -> None: ...

    def record_data_gap(self, gap: DataGap) -> None: ...

    def record_quality_events(self, events: Sequence[DataQualityEvent]) -> int:
        """Persist rejected or suspicious ticks. Nothing is discarded silently."""
        ...

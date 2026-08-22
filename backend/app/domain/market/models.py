"""Market-data domain models.

Provider-neutral. Nothing here knows that Zerodha exists: the adapter converts
broker payloads into these types, and every downstream module (repository,
candle engine, later the strategy engine) consumes only these.

Prices are :class:`~decimal.Decimal`. Broker feeds deliver integer paise-like
values that must be divided by a segment-specific factor; doing that in float
loses exactness on the very values position sizing later depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum

from app.core.time import ensure_utc

__all__ = [
    "Candle",
    "CandleInterval",
    "CandleStatus",
    "DepthLevel",
    "Exchange",
    "Instrument",
    "InstrumentType",
    "MarketDepth",
    "MarketTick",
    "Segment",
    "TickMode",
]


class Exchange(StrEnum):
    """Exchanges the instrument dump can report."""

    NSE = "NSE"
    NFO = "NFO"
    CDS = "CDS"
    BSE = "BSE"
    BFO = "BFO"
    BCD = "BCD"
    MCX = "MCX"
    NCO = "NCO"
    BCO = "BCO"


class Segment(StrEnum):
    """Segments this phase cares about; others are carried through as-is."""

    NSE = "NSE"
    BSE = "BSE"
    INDICES = "INDICES"


class InstrumentType(StrEnum):
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"


class TickMode(StrEnum):
    """Subscription modes, mirroring the broker's streaming modes.

    Which fields a tick carries depends on the mode, so every optional field on
    :class:`MarketTick` is genuinely optional rather than defensively typed.
    """

    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"


class CandleInterval(StrEnum):
    """Bar intervals.

    Only 1m is built from ticks. Larger intervals are aggregations of completed
    1-minute bars, never independently derived from ticks - two derivations of
    the same bar will disagree at the edges, and the disagreement shows up as
    an unreproducible backtest.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"

    @property
    def seconds(self) -> int:
        return {"1m": 60, "5m": 300, "15m": 900}[self.value]

    @property
    def delta(self) -> timedelta:
        return timedelta(seconds=self.seconds)

    @property
    def base_multiple(self) -> int:
        """How many 1-minute bars make up one bar of this interval."""
        return self.seconds // 60


class CandleStatus(StrEnum):
    """A bar is only safe to act on once it is COMPLETED.

    Acting on an in-progress bar is repainting: the values change under you and
    a backtest can never reproduce what the strategy actually saw.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DepthSide(IntEnum):
    BID = 0
    ASK = 1


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradable (or index) instrument from the broker's instrument dump.

    ``instrument_token`` is the streaming subscription key, but it is **not** a
    stable identity: exchanges reuse tokens for derivatives after expiry. The
    stable key is ``(exchange, tradingsymbol)``.
    """

    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    exchange: str
    segment: str
    instrument_type: str
    tick_size: Decimal
    lot_size: int
    expiry: date | None = None
    strike: Decimal | None = None
    last_price: Decimal | None = None
    source: str = "unknown"
    retrieved_at: datetime | None = None

    @property
    def key(self) -> str:
        """Stable identity: exchange + trading symbol."""
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def is_index(self) -> bool:
        """Indices stream a different packet layout and cannot be traded directly."""
        return self.segment.upper() == Segment.INDICES

    @property
    def is_tradable(self) -> bool:
        return not self.is_index


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: Decimal
    quantity: int
    orders: int


@dataclass(frozen=True, slots=True)
class MarketDepth:
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()

    @property
    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None


@dataclass(frozen=True, slots=True)
class MarketTick:
    """A normalized tick.

    Only ``instrument_token``, ``last_price``, ``mode`` and ``received_at`` are
    always present. Everything else depends on the subscription mode and on
    whether the instrument is an index, so absence is modelled explicitly
    instead of being filled with zeroes.
    """

    instrument_token: int
    last_price: Decimal
    mode: TickMode
    received_at: datetime
    exchange: str | None = None
    tradingsymbol: str | None = None
    exchange_timestamp: datetime | None = None
    last_traded_at: datetime | None = None
    last_quantity: int | None = None
    average_price: Decimal | None = None
    volume: int | None = None
    total_buy_quantity: int | None = None
    total_sell_quantity: int | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    open_interest: int | None = None
    depth: MarketDepth | None = None
    is_index: bool = False
    source: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        if self.exchange_timestamp is not None:
            object.__setattr__(self, "exchange_timestamp", ensure_utc(self.exchange_timestamp))
        if self.last_traded_at is not None:
            object.__setattr__(self, "last_traded_at", ensure_utc(self.last_traded_at))

    @property
    def event_time(self) -> datetime:
        """The timestamp used for bucketing and ordering.

        The exchange timestamp when the feed supplies one, otherwise our receive
        time. LTP mode and index quote mode carry no exchange timestamp at all,
        so a fallback is required rather than optional.
        """
        return self.exchange_timestamp or self.received_at

    @property
    def has_exchange_time(self) -> bool:
        return self.exchange_timestamp is not None


@dataclass(frozen=True, slots=True)
class Candle:
    """An OHLCV bar.

    ``status`` is load-bearing: an IN_PROGRESS bar is a live view that will
    still change, a COMPLETED bar never changes again.
    """

    instrument_token: int
    interval: CandleInterval
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    status: CandleStatus
    tick_count: int = 0
    source: str = "unknown"
    tradingsymbol: str | None = None
    exchange: str | None = None
    last_update_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_at", ensure_utc(self.start_at))
        object.__setattr__(self, "end_at", ensure_utc(self.end_at))
        if self.last_update_at is not None:
            object.__setattr__(self, "last_update_at", ensure_utc(self.last_update_at))

    @property
    def is_completed(self) -> bool:
        return self.status is CandleStatus.COMPLETED

    @property
    def range(self) -> Decimal:
        return self.high - self.low


@dataclass(slots=True)
class _CandleAccumulator:
    """Mutable builder behind the immutable :class:`Candle` snapshots."""

    instrument_token: int
    interval: CandleInterval
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    tick_count: int = 0
    source: str = "unknown"
    tradingsymbol: str | None = None
    exchange: str | None = None
    last_update_at: datetime | None = None
    _volume_baseline: int | None = field(default=None, repr=False)

    def snapshot(self, status: CandleStatus) -> Candle:
        return Candle(
            instrument_token=self.instrument_token,
            interval=self.interval,
            start_at=self.start_at,
            end_at=self.end_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            status=status,
            tick_count=self.tick_count,
            source=self.source,
            tradingsymbol=self.tradingsymbol,
            exchange=self.exchange,
            last_update_at=self.last_update_at,
        )

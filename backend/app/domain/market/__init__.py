"""Market-data domain.

Implemented in Phase 1:
  * provider-neutral models: Instrument, MarketTick, Candle
  * ports: MarketDataProvider, InstrumentSource, MarketDataRepository
  * tick data-quality validation
  * tick -> 1m -> 5m/15m candle aggregation
  * market-session abstraction (NSE equity)
  * explicit, configurable tradable universe

Still to come:
  * daily universe eligibility filters - liquidity, ban list, events (Phase 1+)
  * versioned incremental indicators (Phase 2)
  * deterministic market-regime classification (Phase 2)

Nothing in this package imports a broker adapter.
"""

from app.domain.market.candles import CandleEngine, bucket_start
from app.domain.market.models import (
    Candle,
    CandleInterval,
    CandleStatus,
    Instrument,
    MarketDepth,
    MarketTick,
    TickMode,
)
from app.domain.market.ports import (
    ConnectionEvent,
    ConnectionEventType,
    DataGap,
    MarketDataProvider,
    MarketDataRepository,
    ProviderState,
    TickBatch,
)
from app.domain.market.quality import (
    DataQualityEvent,
    DataQualityIssue,
    TickValidator,
)
from app.domain.market.session import MarketSessionCalendar, SessionState
from app.domain.market.universe import (
    UniverseEntry,
    UniverseResolution,
    parse_universe,
    resolve_universe,
)

__all__ = [
    "Candle",
    "CandleEngine",
    "CandleInterval",
    "CandleStatus",
    "ConnectionEvent",
    "ConnectionEventType",
    "DataGap",
    "DataQualityEvent",
    "DataQualityIssue",
    "Instrument",
    "MarketDataProvider",
    "MarketDataRepository",
    "MarketDepth",
    "MarketSessionCalendar",
    "MarketTick",
    "ProviderState",
    "SessionState",
    "TickBatch",
    "TickMode",
    "TickValidator",
    "UniverseEntry",
    "UniverseResolution",
    "bucket_start",
    "parse_universe",
    "resolve_universe",
]

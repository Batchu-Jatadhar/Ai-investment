"""Instrument master.

The broker publishes a complete instrument dump once a day. This service owns
refreshing it, validating it, caching lookups and - importantly - knowing when
it has gone stale.

Staleness matters: lot sizes, tick sizes and tradable symbols change, and an
expired derivative's token can be reissued to a different contract. Acting on
yesterday's master is a real hazard, so ``is_stale`` is checked before the
universe is resolved rather than discovered later.

Instrument tokens are never hard-coded anywhere. Everything resolves through
``(exchange, tradingsymbol)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.core.time import Clock, SystemClock
from app.domain.market.models import Instrument
from app.domain.market.ports import InstrumentSource, MarketDataRepository
from app.domain.market.universe import (
    UniverseEntry,
    UniverseResolution,
    parse_universe,
    resolve_universe,
)

logger = get_logger(__name__)

__all__ = ["InstrumentMaster", "InstrumentValidation", "RefreshResult"]


@dataclass(frozen=True, slots=True)
class InstrumentValidation:
    """Findings from checking a freshly fetched dump before it is stored."""

    total: int = 0
    duplicate_symbols: tuple[str, ...] = ()
    duplicate_tokens: tuple[int, ...] = ()
    missing_tick_size: tuple[str, ...] = ()
    zero_lot_size: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """A dump with duplicate identities cannot be trusted for lookups."""
        return self.total > 0 and not self.duplicate_symbols

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "duplicate_symbols": len(self.duplicate_symbols),
            "duplicate_tokens": len(self.duplicate_tokens),
            "missing_tick_size": len(self.missing_tick_size),
            "zero_lot_size": len(self.zero_lot_size),
            "usable": self.is_usable,
        }


@dataclass(frozen=True, slots=True)
class RefreshResult:
    stored: int
    retrieved_at: datetime
    validation: InstrumentValidation
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "stored": self.stored,
            "retrieved_at": self.retrieved_at.isoformat(),
            "source": self.source,
            "validation": self.validation.as_dict(),
        }


def validate_instruments(instruments: Sequence[Instrument]) -> InstrumentValidation:
    seen_symbols: dict[str, int] = {}
    seen_tokens: dict[int, int] = {}
    duplicate_symbols: list[str] = []
    duplicate_tokens: list[int] = []
    missing_tick: list[str] = []
    zero_lot: list[str] = []

    for instrument in instruments:
        key = instrument.key
        seen_symbols[key] = seen_symbols.get(key, 0) + 1
        if seen_symbols[key] == 2:
            duplicate_symbols.append(key)

        token = instrument.instrument_token
        seen_tokens[token] = seen_tokens.get(token, 0) + 1
        if seen_tokens[token] == 2:
            duplicate_tokens.append(token)

        if instrument.tick_size <= 0 and instrument.is_tradable:
            missing_tick.append(key)
        if instrument.lot_size <= 0 and instrument.is_tradable:
            zero_lot.append(key)

    return InstrumentValidation(
        total=len(instruments),
        duplicate_symbols=tuple(duplicate_symbols),
        duplicate_tokens=tuple(duplicate_tokens),
        missing_tick_size=tuple(missing_tick[:50]),
        zero_lot_size=tuple(zero_lot[:50]),
    )


class InstrumentMasterError(RuntimeError):
    """Raised when a refresh produced an unusable dump."""


class InstrumentMaster:
    """Refresh, validate, cache and serve instrument metadata."""

    def __init__(
        self,
        repository: MarketDataRepository,
        source: InstrumentSource | None = None,
        *,
        clock: Clock | None = None,
        max_age: timedelta = timedelta(hours=24),
    ) -> None:
        self._repository = repository
        self._source = source
        self._clock = clock or SystemClock()
        self._max_age = max_age
        self._by_token: dict[int, Instrument] = {}
        self._by_symbol: dict[str, Instrument] = {}
        self._loaded_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # refresh and load
    # ------------------------------------------------------------------ #

    async def refresh(self, exchange: str | None = None) -> RefreshResult:
        """Fetch a fresh dump, validate it, then replace the stored master."""
        if self._source is None:
            raise InstrumentMasterError(
                "no instrument source is configured; supply credentials or use "
                "a replay/offline source"
            )
        instruments = await self._source.fetch_instruments(exchange)
        validation = validate_instruments(instruments)
        if not validation.is_usable:
            raise InstrumentMasterError(f"instrument dump is unusable: {validation.as_dict()}")

        retrieved_at = self._clock.now()
        stored = self._repository.replace_instruments(instruments, retrieved_at)
        self._index(instruments)
        self._loaded_at = retrieved_at

        logger.info(
            "instrument_master_refreshed",
            extra={
                "stored": stored,
                "source": getattr(self._source, "name", "unknown"),
                **validation.as_dict(),
            },
        )
        return RefreshResult(
            stored=stored,
            retrieved_at=retrieved_at,
            validation=validation,
            source=getattr(self._source, "name", "unknown"),
        )

    def load_from_repository(self) -> int:
        """Populate the lookup cache from what is already stored."""
        instruments = self._repository.all_instruments()
        self._index(instruments)
        self._loaded_at = self._repository.instruments_retrieved_at()
        return len(instruments)

    def _index(self, instruments: Sequence[Instrument]) -> None:
        self._by_token = {i.instrument_token: i for i in instruments}
        self._by_symbol = {i.key.upper(): i for i in instruments}

    # ------------------------------------------------------------------ #
    # lookup
    # ------------------------------------------------------------------ #

    def by_token(self, instrument_token: int) -> Instrument | None:
        cached = self._by_token.get(instrument_token)
        if cached is not None:
            return cached
        found = self._repository.get_instrument_by_token(instrument_token)
        if found is not None:
            self._by_token[instrument_token] = found
        return found

    def by_symbol(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        key = f"{exchange.strip().upper()}:{tradingsymbol.strip().upper()}"
        cached = self._by_symbol.get(key)
        if cached is not None:
            return cached
        found = self._repository.get_instrument_by_symbol(exchange, tradingsymbol)
        if found is not None:
            self._by_symbol[key] = found
        return found

    # ------------------------------------------------------------------ #
    # freshness
    # ------------------------------------------------------------------ #

    @property
    def retrieved_at(self) -> datetime | None:
        return self._loaded_at or self._repository.instruments_retrieved_at()

    def age(self, now: datetime | None = None) -> timedelta | None:
        retrieved = self.retrieved_at
        if retrieved is None:
            return None
        return (now or self._clock.now()) - retrieved

    def is_stale(self, now: datetime | None = None) -> bool:
        """True when the master is missing or older than the configured limit."""
        age = self.age(now)
        return age is None or age > self._max_age

    @property
    def count(self) -> int:
        return len(self._by_token) or self._repository.instrument_count()

    # ------------------------------------------------------------------ #
    # universe
    # ------------------------------------------------------------------ #

    def resolve(self, entries: Sequence[UniverseEntry] | str) -> UniverseResolution:
        """Resolve a configured universe to concrete instruments."""
        parsed = parse_universe(entries) if isinstance(entries, str) else tuple(entries)
        resolution = resolve_universe(parsed, self.by_symbol)
        if resolution.unresolved:
            logger.warning(
                "universe_entries_unresolved",
                extra={"unresolved": [e.key for e in resolution.unresolved]},
            )
        return resolution

    # ------------------------------------------------------------------ #

    def status(self, now: datetime | None = None) -> dict[str, object]:
        moment = now or self._clock.now()
        age = self.age(moment)
        retrieved = self.retrieved_at
        return {
            "count": self.count,
            "retrieved_at": retrieved.isoformat() if retrieved else None,
            "age_seconds": round(age.total_seconds(), 1) if age else None,
            "max_age_seconds": self._max_age.total_seconds(),
            "stale": self.is_stale(moment),
            "source": getattr(self._source, "name", None),
        }

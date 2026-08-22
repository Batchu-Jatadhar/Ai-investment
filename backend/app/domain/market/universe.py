"""Tradable universe configuration and resolution.

The universe is explicit configuration, never inferred and never hard-coded as
instrument tokens: tokens are reused by the exchange after derivative expiry, so
a token literal in source is a defect waiting for an expiry date.

Entries are written as ``EXCHANGE:TRADINGSYMBOL`` and resolved through the
instrument master at startup.

Index entries (``NSE:NIFTY 50``, ``NSE:NIFTY BANK``) are legitimate universe
members for *data* purposes but are flagged ``is_index`` and are not tradable -
index exposure requires derivatives, which are out of scope. Resolution
therefore reports them separately rather than pretending they can be traded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from app.domain.market.models import Instrument

__all__ = [
    "LookupBySymbol",
    "UniverseEntry",
    "UniverseResolution",
    "UniverseSpecError",
    "parse_universe",
    "resolve_universe",
]

LookupBySymbol = Callable[[str, str], "Instrument | None"]
"""Signature of an instrument-master lookup: (exchange, tradingsymbol) -> Instrument."""


class UniverseSpecError(ValueError):
    """Raised when a universe entry is not ``EXCHANGE:TRADINGSYMBOL``."""


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    exchange: str
    tradingsymbol: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @classmethod
    def parse(cls, raw: str) -> UniverseEntry:
        text = raw.strip()
        if not text:
            raise UniverseSpecError("empty universe entry")
        if ":" not in text:
            raise UniverseSpecError(
                f"universe entry {raw!r} must be EXCHANGE:TRADINGSYMBOL, for example NSE:RELIANCE"
            )
        exchange, _, symbol = text.partition(":")
        exchange = exchange.strip().upper()
        symbol = symbol.strip().upper()
        if not exchange or not symbol:
            raise UniverseSpecError(f"universe entry {raw!r} must be EXCHANGE:TRADINGSYMBOL")
        return cls(exchange=exchange, tradingsymbol=symbol)


def parse_universe(raw: str | Iterable[str]) -> tuple[UniverseEntry, ...]:
    """Parse a comma-separated (or already split) universe specification."""
    items: Iterable[str]
    items = raw.split(",") if isinstance(raw, str) else raw
    entries: list[UniverseEntry] = []
    seen: set[str] = set()
    for item in items:
        if not item or not item.strip():
            continue
        entry = UniverseEntry.parse(item)
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class UniverseResolution:
    """Outcome of resolving a configured universe against the instrument master."""

    resolved: tuple[Instrument, ...] = ()
    unresolved: tuple[UniverseEntry, ...] = ()
    indices: tuple[Instrument, ...] = field(default=())

    @property
    def tokens(self) -> tuple[int, ...]:
        return tuple(instrument.instrument_token for instrument in self.resolved)

    @property
    def tradable(self) -> tuple[Instrument, ...]:
        return tuple(i for i in self.resolved if i.is_tradable)

    @property
    def is_complete(self) -> bool:
        return not self.unresolved

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved": [i.key for i in self.resolved],
            "indices": [i.key for i in self.indices],
            "unresolved": [e.key for e in self.unresolved],
            "token_count": len(self.tokens),
        }


def resolve_universe(
    entries: Sequence[UniverseEntry],
    lookup: LookupBySymbol,
) -> UniverseResolution:
    """Resolve entries to instruments. Unknown entries are reported, not dropped."""
    resolved: list[Instrument] = []
    unresolved: list[UniverseEntry] = []
    indices: list[Instrument] = []

    for entry in entries:
        instrument = lookup(entry.exchange, entry.tradingsymbol)
        if instrument is None:
            unresolved.append(entry)
            continue
        resolved.append(instrument)
        if instrument.is_index:
            indices.append(instrument)

    return UniverseResolution(
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
        indices=tuple(indices),
    )

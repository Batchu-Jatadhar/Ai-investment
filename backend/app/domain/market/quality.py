"""Tick data-quality validation.

Nothing is discarded silently. Every rejected or suspicious tick produces a
:class:`DataQualityEvent` that is counted and persisted, so a feed that starts
misbehaving is visible before it corrupts a bar.

Logging policy: individual ticks are never logged. Issues are counted, and the
counters are what surface in health and metrics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.domain.market.models import Instrument, MarketTick, TickMode

__all__ = [
    "DataQualityEvent",
    "DataQualityIssue",
    "QualityCounters",
    "TickValidator",
    "ValidationResult",
]


class DataQualityIssue(StrEnum):
    MALFORMED_MESSAGE = "malformed_message"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    DUPLICATE_TICK = "duplicate_tick"
    OUT_OF_ORDER = "out_of_order"
    STALE_TICK = "stale_tick"
    OUTSIDE_SESSION = "outside_session"
    VOLUME_REGRESSION = "volume_regression"

    @property
    def rejects_tick(self) -> bool:
        """Whether the issue makes the tick unusable.

        Flag-only issues are still recorded; they mark data as suspect without
        throwing away a price we may need.
        """
        return self in {
            DataQualityIssue.MALFORMED_MESSAGE,
            DataQualityIssue.UNKNOWN_INSTRUMENT,
            DataQualityIssue.INVALID_PRICE,
            DataQualityIssue.INVALID_QUANTITY,
            DataQualityIssue.DUPLICATE_TICK,
            DataQualityIssue.OUT_OF_ORDER,
        }


@dataclass(frozen=True, slots=True)
class DataQualityEvent:
    issue: DataQualityIssue
    occurred_at: datetime
    instrument_token: int | None = None
    provider: str = "unknown"
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    tick: MarketTick | None
    issues: tuple[DataQualityEvent, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.tick is not None

    @property
    def issue_kinds(self) -> tuple[DataQualityIssue, ...]:
        return tuple(event.issue for event in self.issues)


@dataclass(slots=True)
class QualityCounters:
    """Aggregate counters. Cheap to read, safe to expose in health."""

    received: int = 0
    accepted: int = 0
    rejected: int = 0
    by_issue: dict[str, int] = field(default_factory=dict)

    def record(self, issue: DataQualityIssue) -> None:
        self.by_issue[issue.value] = self.by_issue.get(issue.value, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "by_issue": dict(self.by_issue),
        }


@dataclass(slots=True)
class _TokenState:
    last_event_time: datetime | None = None
    last_price: Decimal | None = None
    last_volume: int | None = None
    last_signature: tuple[object, ...] | None = None


InstrumentResolver = Callable[[int], Instrument | None]


class TickValidator:
    """Validates and de-duplicates a tick stream, per instrument.

    Stateful by design: duplicate, ordering and volume-regression checks need
    the previous accepted tick for that instrument.
    """

    def __init__(
        self,
        resolve_instrument: InstrumentResolver,
        *,
        provider: str = "unknown",
        stale_after: timedelta = timedelta(seconds=30),
        max_price: Decimal = Decimal("100000000"),
    ) -> None:
        self._resolve = resolve_instrument
        self._provider = provider
        self._stale_after = stale_after
        self._max_price = max_price
        self._state: dict[int, _TokenState] = {}
        self.counters = QualityCounters()

    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Forget per-instrument history (used after a gap, before replay)."""
        self._state.clear()

    def validate(self, tick: MarketTick, *, now: datetime) -> ValidationResult:
        self.counters.received += 1
        issues: list[DataQualityEvent] = []

        def flag(issue: DataQualityIssue, **detail: object) -> None:
            issues.append(
                DataQualityEvent(
                    issue=issue,
                    occurred_at=now,
                    instrument_token=tick.instrument_token,
                    provider=self._provider,
                    detail=detail,
                )
            )
            self.counters.record(issue)

        instrument = self._resolve(tick.instrument_token)
        if instrument is None:
            flag(DataQualityIssue.UNKNOWN_INSTRUMENT, token=tick.instrument_token)
            return self._reject(issues)

        if tick.last_price <= 0 or tick.last_price > self._max_price:
            flag(DataQualityIssue.INVALID_PRICE, price=str(tick.last_price))
            return self._reject(issues)

        for name, value in (
            ("last_quantity", tick.last_quantity),
            ("volume", tick.volume),
            ("total_buy_quantity", tick.total_buy_quantity),
            ("total_sell_quantity", tick.total_sell_quantity),
        ):
            if value is not None and value < 0:
                flag(DataQualityIssue.INVALID_QUANTITY, field=name, value=value)
                return self._reject(issues)

        # FULL mode is documented to carry an exchange timestamp; its absence is
        # a real anomaly. LTP and index-quote modes legitimately have none, so
        # the fallback to receive time is expected there, not a defect.
        if tick.exchange_timestamp is None and tick.mode is TickMode.FULL:
            flag(DataQualityIssue.MISSING_TIMESTAMP, mode=tick.mode.value)

        event_time = tick.event_time
        state = self._state.setdefault(tick.instrument_token, _TokenState())

        signature = (event_time, tick.last_price, tick.volume, tick.last_quantity)
        if state.last_signature is not None and signature == state.last_signature:
            flag(DataQualityIssue.DUPLICATE_TICK, event_time=event_time.isoformat())
            return self._reject(issues)

        if state.last_event_time is not None and event_time < state.last_event_time:
            flag(
                DataQualityIssue.OUT_OF_ORDER,
                event_time=event_time.isoformat(),
                previous=state.last_event_time.isoformat(),
            )
            return self._reject(issues)

        age = (now - event_time).total_seconds()
        if age > self._stale_after.total_seconds():
            flag(DataQualityIssue.STALE_TICK, age_seconds=round(age, 3))

        if (
            tick.volume is not None
            and state.last_volume is not None
            and tick.volume < state.last_volume
        ):
            # Cumulative day volume must not decrease within a session.
            flag(
                DataQualityIssue.VOLUME_REGRESSION,
                volume=tick.volume,
                previous=state.last_volume,
            )

        state.last_event_time = event_time
        state.last_price = tick.last_price
        if tick.volume is not None:
            state.last_volume = tick.volume
        state.last_signature = signature

        self.counters.accepted += 1
        return ValidationResult(tick=tick, issues=tuple(issues))

    # ------------------------------------------------------------------ #

    def _reject(self, issues: list[DataQualityEvent]) -> ValidationResult:
        self.counters.rejected += 1
        return ValidationResult(tick=None, issues=tuple(issues))

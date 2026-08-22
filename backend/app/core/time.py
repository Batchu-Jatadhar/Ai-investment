"""Canonical time handling.

One convention, applied everywhere:

*   **Storage and processing use timezone-aware UTC.** Every ``datetime`` that
    crosses a module boundary carries ``tzinfo``. Naive datetimes are rejected,
    never coerced by guesswork.
*   **Asia/Kolkata is used only for market-session logic and display.** The
    conversion happens at the edge that needs it, never in the middle of the
    pipeline.

Time is obtained from an injected :class:`Clock`, never from a direct
``datetime.now()`` call at a use site. That is what makes session logic,
staleness detection and candle boundaries testable and, later, replayable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
"""Indian market timezone. Used for session boundaries and display only."""

__all__ = [
    "IST",
    "UTC",
    "Clock",
    "FixedClock",
    "NaiveDatetimeError",
    "SystemClock",
    "ensure_utc",
    "ist_datetime",
    "to_ist",
    "utc_now",
]


class NaiveDatetimeError(ValueError):
    """Raised when a naive datetime reaches code that requires an offset.

    Guessing the intended zone is how UTC and local timestamps get mixed, so
    this is always an error rather than a silent conversion.
    """

    def __init__(self, value: datetime) -> None:
        super().__init__(
            f"naive datetime {value!r} has no timezone; all timestamps must be "
            "timezone-aware (store UTC, convert to IST only for session logic)"
        )


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC, rejecting naive input."""
    if value.tzinfo is None:
        raise NaiveDatetimeError(value)
    return value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    """Convert an aware datetime to Asia/Kolkata."""
    if value.tzinfo is None:
        raise NaiveDatetimeError(value)
    return value.astimezone(IST)


def ist_datetime(day: date, at: time) -> datetime:
    """Build an aware IST datetime from an IST calendar date and wall time."""
    return datetime.combine(day, at, tzinfo=IST)


def utc_now() -> datetime:
    """Current time as aware UTC.

    Prefer injecting a :class:`Clock`; this exists for the composition root and
    for the default :class:`SystemClock`.
    """
    return datetime.now(UTC)


class Clock(Protocol):
    """Source of the current time.

    Injected so that session logic, staleness checks and candle boundaries can
    be driven deterministically in tests and, later, in replay.
    """

    def now(self) -> datetime:
        """Current time as aware UTC."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return utc_now()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SystemClock()"


class FixedClock:
    """A controllable clock for tests and replay.

    Not a fake data source - it only controls *time*. Market data always comes
    from a named provider (see ``ReplayMarketDataProvider``).
    """

    __slots__ = ("_now",)

    def __init__(self, now: datetime) -> None:
        self._now = ensure_utc(now)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FixedClock({self._now.isoformat()})"

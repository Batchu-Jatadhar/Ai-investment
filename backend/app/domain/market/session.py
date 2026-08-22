"""Market-session abstraction.

Kept out of the WebSocket client on purpose: the socket's job is bytes in,
ticks out. Whether the market is open is a domain question, and it is asked by
the candle engine, the staleness monitor and (later) the risk engine.

Phase 1 implements only what is needed to identify the normal NSE equity
session correctly. The shape is extensible - a holiday feed, per-instrument
sessions and special sessions plug in without changing callers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum

from app.core.time import IST, ensure_utc, ist_datetime, to_ist

__all__ = [
    "NSE_EQUITY_SESSION",
    "MarketSessionCalendar",
    "SessionState",
    "SessionWindow",
]


class SessionState(StrEnum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    OPEN = "open"
    POST_CLOSE = "post_close"

    @property
    def accepts_ticks(self) -> bool:
        """Ticks outside these states are unexpected and worth flagging."""
        return self in (SessionState.PRE_OPEN, SessionState.OPEN)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """Wall-clock session boundaries, expressed in IST."""

    name: str
    pre_open_start: time
    open_time: time
    close_time: time
    post_close_end: time

    def state_at(self, ist_time: time) -> SessionState:
        if self.pre_open_start <= ist_time < self.open_time:
            return SessionState.PRE_OPEN
        if self.open_time <= ist_time < self.close_time:
            return SessionState.OPEN
        if self.close_time <= ist_time < self.post_close_end:
            return SessionState.POST_CLOSE
        return SessionState.CLOSED


NSE_EQUITY_SESSION = SessionWindow(
    name="NSE_EQUITY",
    pre_open_start=time(9, 0),
    open_time=time(9, 15),
    close_time=time(15, 30),
    post_close_end=time(16, 0),
)
"""Normal NSE equity session, in IST. Special sessions are out of scope here."""


@dataclass(frozen=True, slots=True)
class MarketSessionCalendar:
    """Answers session questions for a given instant.

    Holidays are an injected set rather than a built-in calendar: a wrong
    hard-coded holiday list is worse than none, because it silently suppresses
    real trading days. Supply the exchange's published list when it matters.
    """

    window: SessionWindow = NSE_EQUITY_SESSION
    holidays: frozenset[date] = field(default_factory=frozenset)
    weekend_days: frozenset[int] = frozenset({5, 6})  # Sat, Sun in IST

    @classmethod
    def nse_equity(cls, holidays: Iterable[date] = ()) -> MarketSessionCalendar:
        return cls(window=NSE_EQUITY_SESSION, holidays=frozenset(holidays))

    # ------------------------------------------------------------------ #

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_days and day not in self.holidays

    def state_at(self, moment: datetime) -> SessionState:
        """Session state at an aware instant."""
        local = to_ist(moment)
        if not self.is_trading_day(local.date()):
            return SessionState.CLOSED
        return self.window.state_at(local.time())

    def is_open(self, moment: datetime) -> bool:
        return self.state_at(moment) is SessionState.OPEN

    def accepts_ticks(self, moment: datetime) -> bool:
        return self.state_at(moment).accepts_ticks

    # ------------------------------------------------------------------ #

    def session_bounds(self, moment: datetime) -> tuple[datetime, datetime] | None:
        """UTC open/close bounds of the trading day containing ``moment``.

        ``None`` when that day is not a trading day.
        """
        local = to_ist(moment)
        if not self.is_trading_day(local.date()):
            return None
        day = local.date()
        opens = ist_datetime(day, self.window.open_time)
        closes = ist_datetime(day, self.window.close_time)
        return ensure_utc(opens), ensure_utc(closes)

    def session_open_at(self, moment: datetime) -> datetime | None:
        bounds = self.session_bounds(moment)
        return bounds[0] if bounds else None

    def describe(self, moment: datetime) -> dict[str, object]:
        """Health/observability view of the session."""
        local = to_ist(moment)
        state = self.state_at(moment)
        return {
            "window": self.window.name,
            "state": state.value,
            "is_trading_day": self.is_trading_day(local.date()),
            "local_time": local.isoformat(),
            "timezone": str(IST),
        }

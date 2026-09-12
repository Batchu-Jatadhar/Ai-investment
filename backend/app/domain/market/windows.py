"""Splitting a historical date range into request-sized windows.

A provider caps how much history one request may cover, so a long range is
fetched as a sequence of shorter ones. This module decides the windows and
nothing else: it knows no provider, no HTTP and no clock.

Windows are half-open ``[start, end)`` and tile the range exactly - each one
begins where the previous ended - so a bar can fall into only one of them and
none can fall between them. A client that filters its response to the window it
asked for therefore can neither lose nor double-count a bar at a boundary.

The default span is 30 days. The provider's own cap for 1-minute candles is
reported as 60 days but is not in its official documentation, so half of it is
used and the span stays configurable rather than tuned against the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.time import ensure_utc

__all__ = ["DEFAULT_MINUTE_WINDOW", "RequestWindow", "plan_windows"]

#: Agreed maximum span of one 1-minute historical request.
DEFAULT_MINUTE_WINDOW = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RequestWindow:
    """One half-open ``[start, end)`` request range, in UTC."""

    start: datetime
    end: datetime


def plan_windows(
    start: datetime, end: datetime, *, max_span: timedelta = DEFAULT_MINUTE_WINDOW
) -> tuple[RequestWindow, ...]:
    """Contiguous windows covering ``[start, end)``, oldest first.

    Every window spans ``max_span`` except possibly the last, which ends exactly
    at ``end``. Raises ``ValueError`` for a naive datetime, an empty or inverted
    range, or a non-positive ``max_span``.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    if start >= end:
        raise ValueError(f"start ({start.isoformat()}) must precede end ({end.isoformat()})")
    if max_span <= timedelta(0):
        raise ValueError(f"max_span must be positive, got {max_span}")

    windows: list[RequestWindow] = []
    cursor = start
    while cursor < end:
        upper = min(cursor + max_span, end)
        windows.append(RequestWindow(cursor, upper))
        cursor = upper
    return tuple(windows)

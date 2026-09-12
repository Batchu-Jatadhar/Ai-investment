"""Pacing historical requests to Kite's rate limit.

Kite allows at most 3 historical-candle requests per second
(https://kite.trade/docs/connect/v3/exceptions/). A long backfill is dozens of
requests, so they are spaced deliberately rather than sent until Kite answers
429.

Time is injected: a :class:`~app.core.time.Clock` to read it and an async
``sleep`` to wait. Nothing here reads the wall clock or sleeps on its own, so a
test drives the pacing with a fake clock whose ``sleep`` simply advances it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.core.time import Clock

__all__ = [
    "HISTORICAL_REQUESTS_PER_SECOND",
    "RequestThrottle",
    "Sleep",
]

#: An awaitable pause of the given number of seconds - ``asyncio.sleep`` in
#: production, a clock-advancing fake in tests.
Sleep = Callable[[float], Awaitable[None]]

#: Kite's documented limit for the historical candle endpoint.
HISTORICAL_REQUESTS_PER_SECOND = 3


class RequestThrottle:
    """At most ``max_requests`` request starts in any ``period``.

    A sliding window over the most recent start times: a request may begin once
    fewer than ``max_requests`` others began within the preceding ``period``.
    Holds mutable pacing state by design, which is why it lives in the adapter
    and not the domain. One throttle is shared by every request to the same
    endpoint.
    """

    def __init__(
        self,
        clock: Clock,
        sleep: Sleep,
        *,
        max_requests: int = HISTORICAL_REQUESTS_PER_SECOND,
        period: timedelta = timedelta(seconds=1),
    ) -> None:
        if max_requests < 1:
            raise ValueError(f"max_requests must be at least 1, got {max_requests}")
        if period <= timedelta(0):
            raise ValueError(f"period must be positive, got {period}")
        self._clock = clock
        self._sleep = sleep
        self._max = max_requests
        self._period = period
        self._starts: deque[datetime] = deque()

    async def acquire(self) -> None:
        """Wait until another request may start, then record that it has."""
        while True:
            now = self._clock.now()
            while self._starts and now - self._starts[0] >= self._period:
                self._starts.popleft()
            if len(self._starts) < self._max:
                self._starts.append(now)
                return
            # Re-checked after waking: a real sleep can return a hair early.
            await self._sleep((self._starts[0] + self._period - now).total_seconds())

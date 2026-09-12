"""Pacing and retrying historical requests to Kite.

Kite allows at most 3 historical-candle requests per second
(https://kite.trade/docs/connect/v3/exceptions/). A long backfill is dozens of
requests, so they are spaced deliberately rather than sent until Kite answers
429.

Failures that may clear on their own - a 429, a transport failure, a 5xx - are
retried a bounded number of times with deterministic exponential backoff.
Everything else fails at once: an expired token needs a human, and a bad request
or a malformed response will be exactly as bad on the next attempt.

Time is injected: a :class:`~app.core.time.Clock` to read it and an async
``sleep`` to wait. Nothing here reads the wall clock or sleeps on its own, so a
test drives the pacing with a fake clock whose ``sleep`` simply advances it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from app.adapters.zerodha.errors import ZerodhaNetworkError, ZerodhaRateLimitedError
from app.core.logging import get_logger
from app.core.time import Clock

logger = get_logger(__name__)

__all__ = [
    "HISTORICAL_REQUESTS_PER_SECOND",
    "RETRYABLE_ERRORS",
    "RequestThrottle",
    "RetryPolicy",
    "Sleep",
    "with_retries",
]

T = TypeVar("T")

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


#: The failures worth another attempt. ``ZerodhaNetworkError`` is what the
#: existing taxonomy makes of transport failures, 5xx responses and Kite's
#: Network/Data/General exceptions. Auth, input and protocol errors - and any
#: plain ``ValueError`` from argument checking - are deliberately absent.
RETRYABLE_ERRORS: tuple[type[Exception], ...] = (ZerodhaRateLimitedError, ZerodhaNetworkError)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff, without jitter so every run waits the same.

    ``max_attempts`` counts the first try, so the default makes at most four
    requests and waits 1 s, 2 s and 4 s between them.
    """

    max_attempts: int = 4
    initial_delay: timedelta = timedelta(seconds=1)
    factor: int = 2
    max_delay: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")
        if self.initial_delay <= timedelta(0):
            raise ValueError(f"initial_delay must be positive, got {self.initial_delay}")
        if self.factor < 1:
            raise ValueError(f"factor must be at least 1, got {self.factor}")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay must not be shorter than initial_delay")

    def delay_after(self, failures: int) -> timedelta:
        """The wait after the ``failures``-th consecutive failure."""
        return min(self.initial_delay * self.factor ** (failures - 1), self.max_delay)


async def with_retries(
    operation: Callable[[], Awaitable[T]], *, policy: RetryPolicy, sleep: Sleep
) -> T:
    """Run ``operation``, retrying only :data:`RETRYABLE_ERRORS`.

    When the attempts run out, the last failure is re-raised as it was - typed,
    with its context - rather than wrapped in something less specific.
    ``operation`` is called afresh each attempt, so anything it acquires first
    (a throttle slot) is acquired again for the retry.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except RETRYABLE_ERRORS as exc:
            if attempt == policy.max_attempts:
                raise
            delay = policy.delay_after(attempt)
            logger.warning(
                "historical_request_retrying",
                extra={
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "delay_seconds": delay.total_seconds(),
                    "error": type(exc).__name__,
                },
            )
            await sleep(delay.total_seconds())
    raise AssertionError("unreachable: the final attempt either returns or raises")

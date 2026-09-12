"""Historical request pacing, driven by a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.adapters.zerodha.pacing import RequestThrottle
from app.core.time import FixedClock

T0 = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


class FakeTime:
    """A clock plus a ``sleep`` that advances it instantly, recording each wait."""

    def __init__(self) -> None:
        self.clock = FixedClock(T0)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


async def burst(count: int) -> tuple[list[datetime], list[float]]:
    """``count`` requests fired back to back; when each started, and every wait."""
    time = FakeTime()
    throttle = RequestThrottle(time.clock, time.sleep)
    started = []
    for _ in range(count):
        await throttle.acquire()
        started.append(time.clock.now())
    return started, time.sleeps


async def test_a_burst_is_held_to_three_requests_per_second() -> None:
    started, sleeps = await burst(7)

    second = timedelta(seconds=1)
    assert started == [T0] * 3 + [T0 + second] * 3 + [T0 + 2 * second]
    assert sleeps == [1.0, 1.0]
    # Any four consecutive starts span at least a full second.
    assert all(started[i + 3] - started[i] >= second for i in range(len(started) - 3))


async def test_requests_already_spaced_out_never_wait() -> None:
    time = FakeTime()
    throttle = RequestThrottle(time.clock, time.sleep)
    for _ in range(6):
        await throttle.acquire()
        time.clock.advance(timedelta(milliseconds=400))
    assert time.sleeps == []


async def test_a_partial_wait_covers_only_the_remaining_time() -> None:
    """Three requests at T0, then one 0.25 s later: it waits the other 0.75 s."""
    time = FakeTime()
    throttle = RequestThrottle(time.clock, time.sleep)
    for _ in range(3):
        await throttle.acquire()
    time.clock.advance(timedelta(milliseconds=250))
    await throttle.acquire()
    assert time.sleeps == [0.75]
    assert time.clock.now() == T0 + timedelta(seconds=1)


async def test_pacing_is_deterministic() -> None:
    assert await burst(10) == await burst(10)

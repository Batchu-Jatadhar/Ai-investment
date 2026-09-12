"""Historical request pacing and retries, driven by a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import (
    ZerodhaNetworkError,
    ZerodhaProtocolError,
    ZerodhaRateLimitedError,
    classify_response,
)
from app.adapters.zerodha.pacing import RequestThrottle, RetryPolicy, with_retries
from app.core.time import FixedClock
from app.domain.market.models import CandleInterval, Instrument

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


# --------------------------------------------------------------------------- #
# retries
# --------------------------------------------------------------------------- #


class Flaky:
    """Raises each prepared failure in turn, then returns ``"ok"``."""

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


@pytest.mark.parametrize(
    "transient",
    [
        pytest.param(lambda: classify_response(429, None, "Too many requests"), id="429"),
        pytest.param(
            lambda: ZerodhaNetworkError("ConnectError contacting the broker"), id="network"
        ),
        pytest.param(lambda: classify_response(503, None, "Service unavailable"), id="5xx"),
    ],
)
async def test_transient_failures_are_retried_with_backoff_until_success(transient) -> None:  # noqa: ANN001
    time = FakeTime()
    operation = Flaky(transient(), transient())

    assert await with_retries(operation, policy=RetryPolicy(), sleep=time.sleep) == "ok"
    assert operation.calls == 3
    assert time.sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "permanent",
    [
        pytest.param(classify_response(403, "TokenException", "expired"), id="auth"),
        pytest.param(classify_response(400, "InputException", "bad range"), id="input"),
        pytest.param(
            ZerodhaProtocolError("historical candle row 0 must have 6 fields"), id="protocol"
        ),
        pytest.param(ValueError("interval 'day' is not supported"), id="unsupported-interval"),
    ],
)
async def test_permanent_failures_are_raised_at_once(permanent: Exception) -> None:
    time = FakeTime()
    operation = Flaky(permanent)

    with pytest.raises(type(permanent)) as raised:
        await with_retries(operation, policy=RetryPolicy(), sleep=time.sleep)
    assert raised.value is permanent
    assert (operation.calls, time.sleeps) == (1, [])


async def test_retries_are_bounded_and_the_last_typed_failure_surfaces() -> None:
    time = FakeTime()
    failures = [classify_response(429, None, f"attempt {n}") for n in range(1, 5)]
    operation = Flaky(*failures, ZerodhaRateLimitedError("never reached"))

    with pytest.raises(ZerodhaRateLimitedError) as raised:
        await with_retries(operation, policy=RetryPolicy(max_attempts=4), sleep=time.sleep)
    assert raised.value is failures[-1]
    assert raised.value.context["http_status"] == 429
    assert operation.calls == 4
    assert time.sleeps == [1.0, 2.0, 4.0]

    capped = RetryPolicy(max_attempts=4, factor=10, max_delay=timedelta(seconds=5))
    assert [capped.delay_after(n).total_seconds() for n in (1, 2, 3)] == [1.0, 5.0, 5.0]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"max_attempts": 0}, id="no-attempts"),
        pytest.param({"initial_delay": timedelta(0)}, id="zero-delay"),
        pytest.param({"factor": 0}, id="shrinking-factor"),
        pytest.param({"max_delay": timedelta(milliseconds=500)}, id="cap-below-initial"),
    ],
)
def test_a_nonsensical_policy_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**overrides)  # type: ignore[arg-type]


async def test_a_throttled_client_fetch_retries_a_429_through_a_fresh_throttle_slot() -> None:
    """Composition check: throttle + retry around the real client and transport."""
    time = FakeTime()
    candle_json = (
        '{"status": "success", "data": {"candles": '
        '[["2026-08-21T09:15:00+0530", 100.05, 101, 99, 100.5, 10]]}}'
    )
    responses = [
        httpx.Response(429, json={"status": "error", "message": "slow down"}),
        httpx.Response(200, text=candle_json),
    ]
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    client = ZerodhaRestClient(
        api_key="k",
        access_token="t",
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    throttle = RequestThrottle(time.clock, time.sleep)
    instrument = Instrument(
        instrument_token=738561,
        exchange_token=2885,
        tradingsymbol="RELIANCE",
        name="RELIANCE",
        exchange="NSE",
        segment="NSE",
        instrument_type="EQ",
        tick_size=Decimal("0.05"),
        lot_size=1,
    )

    async def fetch_once():  # noqa: ANN202
        await throttle.acquire()
        return await client.fetch_historical_candles(
            instrument,
            CandleInterval.M1,
            start=T0,
            end=T0 + timedelta(minutes=1),
            as_of=T0 + timedelta(days=1),
        )

    (candle,) = await with_retries(fetch_once, policy=RetryPolicy(), sleep=time.sleep)

    assert len(requests) == 2
    assert candle.open == Decimal("100.05")
    assert time.sleeps == [1.0]  # the retry backoff; the throttle had room both times

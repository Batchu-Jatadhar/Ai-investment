"""The read-only historical backfill command.

Runs the real command, real Zerodha client over ``httpx.MockTransport``, real
ingestion service and SQL repository. Only time is faked.
"""

from __future__ import annotations

import io
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.zerodha.client import ZerodhaRestClient
from app.core.time import FixedClock
from app.runtime.historical_backfill import run
from tests.market.conftest import RELIANCE_TOKEN, make_instrument

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
FRIDAY_ROWS = ", ".join(
    f'["2026-08-21T09:{15 + i}:00+0530", {100 + i}, {101 + i}, {99 + i}, {100 + i}.5, 10]'
    for i in range(15)
)
OK = '{"status": "success", "data": {"candles": [' + FRIDAY_ROWS + "]}}"


class Broker:
    """A mock Kite that records every request and answers with ``answer``."""

    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer
        self.requests: list[httpx.Request] = []
        self.client = ZerodhaRestClient(
            api_key="k",
            access_token="t",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


class AsOfSpy:
    """Wraps a source to record the as_of the service was handed."""

    name = "spy"

    def __init__(self, inner: ZerodhaRestClient) -> None:
        self.inner = inner
        self.as_ofs: list[datetime] = []

    async def fetch_historical_candles(self, instrument, interval, *, start, end, as_of):  # noqa: ANN001, ANN201
        self.as_ofs.append(as_of)
        return await self.inner.fetch_historical_candles(
            instrument, interval, start=start, end=end, as_of=as_of
        )


async def backfill(repository, argv: list[str], source) -> tuple[int, str]:  # noqa: ANN001
    repository.replace_instruments([make_instrument(RELIANCE_TOKEN, "RELIANCE")], NOW)
    clock = FixedClock(NOW)

    async def sleep(seconds: float) -> None:
        clock.advance(timedelta(seconds=seconds))

    out = io.StringIO()
    code = await run(argv, source=source, repository=repository, clock=clock, sleep=sleep, out=out)
    return code, out.getvalue()


RANGE = ["--symbol", "NSE:RELIANCE", "--from", "2026-08-20", "--to", "2026-08-21"]


async def test_a_successful_backfill_reports_counts_and_makes_only_historical_gets(
    repository,  # noqa: ANN001
) -> None:
    broker = Broker(lambda r: httpx.Response(200, text=OK))  # Friday's bars; Thursday has none
    spy = AsOfSpy(broker.client)

    code, output = await backfill(repository, RANGE, spy)

    assert code == 0
    # One 2-day window, as_of taken from the command's clock and passed down.
    assert [(r.method, r.url.path) for r in broker.requests] == [
        ("GET", "/instruments/historical/738561/minute")
    ]
    assert dict(broker.requests[0].url.params) == {
        "from": "2026-08-20 00:00:00",
        "to": "2026-08-22 00:00:00",
    }
    assert spy.as_ofs == [NOW]
    for line in (
        "Historical backfill NSE:RELIANCE (token 738561)",
        "2026-08-20 00:00 IST -> 2026-08-22 00:00 IST",
        "planned 1, ingested 1, no-data 0, failed 0, bars fetched 15",
        "1m         inserted 15, identical 0, conflicts 0",
        "5m         inserted 3, identical 0, conflicts 0",
        "15m        inserted 1, identical 0, conflicts 0",
        "incomplete slots 0",
        "no-data sessions 2026-08-20",
        "status     COMPLETED",
    ):
        assert line in output


async def test_an_explicit_as_of_is_passed_through(repository) -> None:  # noqa: ANN001
    spy = AsOfSpy(Broker(lambda r: httpx.Response(200, text=OK)).client)

    code, _ = await backfill(repository, [*RANGE, "--as-of", "2026-08-21T09:20:00+05:30"], spy)

    assert code == 0
    assert spy.as_ofs == [datetime(2026, 8, 21, 3, 50, tzinfo=UTC)]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["--from", "2026-13-01", "--to", "2026-08-21"], "not a date", id="malformed"),
        pytest.param(
            ["--from", "2026-08-22", "--to", "2026-08-20"], "must be before", id="reversed"
        ),
        pytest.param(
            ["--from", "2026-08-21T09:07:00+05:30", "--to", "2026-08-22"],
            "15-minute boundary",
            id="misaligned",
        ),
        pytest.param(
            ["--from", "2026-08-21T09:15:00", "--to", "2026-08-22"], "no UTC offset", id="naive"
        ),
        pytest.param(
            ["--symbol", "NSE:NOPE", "--from", "2026-08-20", "--to", "2026-08-21"],
            "not in the stored instrument master",
            id="unknown-instrument",
        ),
        pytest.param(
            ["--symbol", "RELIANCE", "--from", "2026-08-20", "--to", "2026-08-21"],
            "EXCHANGE:TRADINGSYMBOL",
            id="bare-symbol",
        ),
    ],
)
async def test_bad_arguments_exit_2_before_any_request(repository, argv, message: str) -> None:  # noqa: ANN001
    broker = Broker(lambda r: httpx.Response(200, text=OK))
    if "--symbol" not in argv:
        argv = ["--symbol", "NSE:RELIANCE", *argv]

    code, output = await backfill(repository, argv, broker.client)

    assert code == 2
    assert message in output
    assert broker.requests == []


async def test_exhausted_retries_exit_1_and_name_the_failed_window(repository) -> None:  # noqa: ANN001
    broker = Broker(lambda r: httpx.Response(429, json={"status": "error", "message": "slow down"}))

    code, output = await backfill(repository, RANGE, broker.client)

    assert code == 1
    assert len(broker.requests) == 4  # the default retry policy's bound
    assert "status     FAILED in window 2026-08-20 00:00 IST -> 2026-08-22 00:00 IST" in output
    assert "ZerodhaRateLimitedError: slow down" in output


async def test_an_expired_token_aborts_with_exit_3(repository) -> None:  # noqa: ANN001
    broker = Broker(
        lambda r: httpx.Response(
            403, json={"status": "error", "message": "expired", "error_type": "TokenException"}
        )
    )

    code, output = await backfill(repository, RANGE, broker.client)

    assert code == 3
    assert len(broker.requests) == 1
    assert "aborted    ZerodhaAuthError: expired" in output


def test_the_module_entry_point_runs_and_describes_itself() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "app.runtime.historical_backfill", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    help_text = " ".join(result.stdout.split())  # argparse wraps lines
    assert result.returncode == 0
    assert "usage: aitrade-backfill" in help_text
    assert "Places no orders" in help_text

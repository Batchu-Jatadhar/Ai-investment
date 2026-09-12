"""Zerodha historical candle client: request shape, parsing, validation, errors.

Driven entirely through ``httpx.MockTransport``; nothing touches the live API.
Times: 2026-08-21 09:15 IST is 03:45 UTC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaInputError,
    ZerodhaNetworkError,
    ZerodhaProtocolError,
    ZerodhaRateLimitedError,
)
from app.domain.market.models import CandleInterval, CandleStatus, Instrument

OPEN_UTC = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)  # 09:15 IST
START = OPEN_UTC
END = OPEN_UTC + timedelta(minutes=15)  # 09:30 IST
AS_OF = datetime(2026, 8, 22, tzinfo=UTC)  # long after the window

RELIANCE = Instrument(
    instrument_token=738561,
    exchange_token=2885,
    tradingsymbol="RELIANCE",
    name="RELIANCE INDUSTRIES",
    exchange="NSE",
    segment="NSE",
    instrument_type="EQ",
    tick_size=Decimal("0.05"),
    lot_size=1,
)


def row(
    hh_mm: str, o: str = "1400.10", h: str = "1402.00", lo: str = "1399.50", c: str = "1401.00"
):  # noqa: ANN201
    return f'["2026-08-21T{hh_mm}:00+0530", {o}, {h}, {lo}, {c}, 1200]'


def body(*rows: str) -> str:
    return '{"status": "success", "data": {"candles": [' + ", ".join(rows) + "]}}"


class Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def client_for(recorder: Recorder) -> ZerodhaRestClient:
    return ZerodhaRestClient(
        api_key="test_key",
        access_token="test_token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )


async def fetch(
    text: str,
    *,
    interval: CandleInterval = CandleInterval.M5,
    start: datetime = START,
    end: datetime = END,
    as_of: datetime = AS_OF,
):  # noqa: ANN201
    recorder = Recorder(httpx.Response(200, text=text))
    candles = await client_for(recorder).fetch_historical_candles(
        RELIANCE, interval, start=start, end=end, as_of=as_of
    )
    return candles, recorder


@pytest.mark.parametrize(
    ("interval", "kite_name"),
    [
        (CandleInterval.M1, "minute"),
        (CandleInterval.M5, "5minute"),
        (CandleInterval.M15, "15minute"),
    ],
)
async def test_request_path_query_and_headers(interval: CandleInterval, kite_name: str) -> None:
    _, recorder = await fetch(body(), interval=interval)

    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.path == f"/instruments/historical/738561/{kite_name}"
    # IST wall-clock bounds, and nothing else - no continuous, no oi.
    assert dict(request.url.params) == {"from": "2026-08-21 09:15:00", "to": "2026-08-21 09:30:00"}
    assert request.url.query == b"from=2026-08-21+09%3A15%3A00&to=2026-08-21+09%3A30%3A00"
    assert request.headers["X-Kite-Version"] == "3"
    assert request.headers["Authorization"] == "token test_key:test_token"


async def test_rows_become_completed_utc_candles_with_exact_decimal_prices() -> None:
    candles, _ = await fetch(
        body(row("09:15", "1400.10", "1402.35", "1399.05", "1401.00"), row("09:20"))
    )

    first = candles[0]
    assert len(candles) == 2
    assert first.start_at == OPEN_UTC and first.start_at.tzinfo is UTC
    assert first.end_at == OPEN_UTC + timedelta(minutes=5)
    # Trailing zeros survive: these were never floats.
    assert [str(p) for p in (first.open, first.high, first.low, first.close)] == [
        "1400.10",
        "1402.35",
        "1399.05",
        "1401.00",
    ]
    assert all(isinstance(p, Decimal) for p in (first.open, first.high, first.low, first.close))
    assert (first.volume, first.status, first.source) == (
        1200,
        CandleStatus.COMPLETED,
        "zerodha_historical",
    )
    assert (first.instrument_token, first.tradingsymbol, first.exchange) == (
        738561,
        "RELIANCE",
        "NSE",
    )
    assert first.interval is CandleInterval.M5


async def test_an_empty_candle_list_is_a_valid_result() -> None:
    candles, _ = await fetch(body())
    assert candles == []


async def test_bars_unfinished_at_as_of_are_dropped() -> None:
    """At 09:24 IST the 09:20 bar is still forming; only 09:15 had closed."""
    candles, _ = await fetch(
        body(row("09:15"), row("09:20")), as_of=OPEN_UTC + timedelta(minutes=9)
    )
    assert [c.start_at for c in candles] == [OPEN_UTC]


async def test_rows_outside_the_requested_window_are_filtered() -> None:
    candles, _ = await fetch(body(row("09:10"), row("09:15"), row("09:25"), row("09:30")))
    assert [c.start_at for c in candles] == [OPEN_UTC, OPEN_UTC + timedelta(minutes=10)]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"status": "success", "data": {}}', id="no-candles-list"),
        pytest.param(body('["2026-08-21T09:15:00+0530", 1400, 1402, 1399, 1401]'), id="short-row"),
        pytest.param(body(row("09:15")[:-1] + ", 5]"), id="long-row"),
        pytest.param(body('["21-08-2026 09:15", 1400, 1402, 1399, 1401, 10]'), id="bad-timestamp"),
        pytest.param(body('["2026-08-21T09:15:00", 1400, 1402, 1399, 1401, 10]'), id="no-offset"),
        pytest.param(body(row("09:15", o='"1400"')), id="non-numeric-open"),
        pytest.param(body(row("09:15", h="1398.00")), id="high-below-low"),
        pytest.param(body(row("09:15", c="1405.00")), id="close-above-high"),
        pytest.param(body(row("09:17")), id="off-5m-grid"),
        pytest.param(body(row("09:15"), row("09:15")), id="duplicate"),
        pytest.param(body(row("09:20"), row("09:15")), id="out-of-order"),
    ],
)
async def test_a_malformed_response_raises_a_protocol_error(text: str) -> None:
    with pytest.raises(ZerodhaProtocolError):
        await fetch(text)


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (400, "InputException", ZerodhaInputError),
        (403, "TokenException", ZerodhaAuthError),
        (429, None, ZerodhaRateLimitedError),
        (500, "GeneralException", ZerodhaNetworkError),
        (502, "NetworkException", ZerodhaNetworkError),
    ],
)
async def test_http_errors_use_the_existing_taxonomy(
    status: int, error_type: str | None, expected: type[Exception]
) -> None:
    payload = json.dumps({"status": "error", "message": "nope", "error_type": error_type})
    recorder = Recorder(httpx.Response(status, text=payload))
    with pytest.raises(expected):
        await client_for(recorder).fetch_historical_candles(
            RELIANCE, CandleInterval.M5, start=START, end=END, as_of=AS_OF
        )


@pytest.mark.parametrize(
    ("interval", "start", "end", "as_of"),
    [
        pytest.param("day", START, END, AS_OF, id="unsupported-interval"),
        pytest.param(CandleInterval.M5, START.replace(tzinfo=None), END, AS_OF, id="naive-start"),
        pytest.param(CandleInterval.M5, START, END, AS_OF.replace(tzinfo=None), id="naive-as-of"),
        pytest.param(CandleInterval.M5, END, START, AS_OF, id="empty-window"),
    ],
)
async def test_invalid_arguments_fail_before_any_request(
    interval: object, start: datetime, end: datetime, as_of: datetime
) -> None:
    recorder = Recorder(httpx.Response(200, text=body()))
    with pytest.raises(ValueError):
        await client_for(recorder).fetch_historical_candles(
            RELIANCE,
            interval,  # type: ignore[arg-type]
            start=start,
            end=end,
            as_of=as_of,
        )
    assert recorder.requests == []

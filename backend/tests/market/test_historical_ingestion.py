"""Historical ingestion service: windows, pacing, aggregation, persistence, failures.

A scripted candle source and fake time; the repository is the real SQL one
from the market-data fixtures.

.. rubric:: Hand-calculated bars

Minute ``i`` of a session (09:15 + i) is open 100 + i, high 101 + i, low 99 + i,
close 100.5 + i, volume 10.

    09:15 5m   open 100, high 105, low 99,  close 104.5, volume 50
    09:15 15m  open 100, high 115, low 99,  close 114.5, volume 150
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaInputError,
    ZerodhaProtocolError,
    classify_response,
)
from app.adapters.zerodha.pacing import RequestThrottle, RetryPolicy
from app.core.time import FixedClock
from app.domain.market.aggregation import IncompleteSlot
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.windows import RequestWindow
from app.services.historical_ingestion import (
    HistoricalIngestionAborted,
    HistoricalIngestionService,
    WindowOutcome,
)
from tests.market.conftest import RELIANCE_TOKEN, make_instrument

M1, M5, M15 = CandleInterval.M1, CandleInterval.M5, CandleInterval.M15
RELIANCE = make_instrument(RELIANCE_TOKEN, "RELIANCE")
DAY = timedelta(days=1)

#: IST midnights, as UTC.
THU = datetime(2026, 8, 19, 18, 30, tzinfo=UTC)
FRI, SAT, SUN = THU + DAY, THU + 2 * DAY, THU + 3 * DAY
OPEN = timedelta(hours=9, minutes=15)  # session open, from an IST midnight
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)


def session(midnight: datetime, count: int = 15, *, skip: tuple[int, ...] = ()) -> list[Candle]:
    bars = []
    for i in range(count):
        if i in skip:
            continue
        start = midnight + OPEN + timedelta(minutes=i)
        bars.append(
            Candle(
                instrument_token=RELIANCE_TOKEN,
                interval=M1,
                start_at=start,
                end_at=start + M1.delta,
                open=Decimal(100 + i),
                high=Decimal(101 + i),
                low=Decimal(99 + i),
                close=Decimal(100 + i) + Decimal("0.5"),
                volume=10,
                status=CandleStatus.COMPLETED,
                source="zerodha_historical",
                tradingsymbol="RELIANCE",
                exchange="NSE",
            )
        )
    return bars


class Script:
    """A candle source answering per window start, and a shared event log."""

    name = "scripted"

    def __init__(self, answers: dict[datetime, list[object]]) -> None:
        self.answers = {start: list(results) for start, results in answers.items()}
        self.events: list[tuple[object, ...]] = []
        self.clock = FixedClock(AS_OF)

    async def fetch_historical_candles(self, instrument, interval, *, start, end, as_of):  # noqa: ANN001, ANN201
        assert (instrument, interval, as_of) == (RELIANCE, M1, AS_OF)
        self.events.append(("fetch", start))
        queue = self.answers.get(start, [[]])
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return list(result)

    async def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))
        self.clock.advance(timedelta(seconds=seconds))


class LoggingThrottle(RequestThrottle):
    def __init__(self, script: Script) -> None:
        super().__init__(script.clock, script.sleep)
        self.script = script

    async def acquire(self) -> None:
        self.script.events.append(("acquire",))
        await super().acquire()


def service(script: Script, repository, max_attempts: int = 2) -> HistoricalIngestionService:  # noqa: ANN001
    return HistoricalIngestionService(
        source=script,
        repository=repository,
        throttle=LoggingThrottle(script),
        retry_policy=RetryPolicy(max_attempts=max_attempts),
        sleep=script.sleep,
        max_window=DAY,
    )


def stored(repository, interval: CandleInterval, start: datetime, end: datetime) -> list[Candle]:  # noqa: ANN001
    return list(
        repository.candles_page(RELIANCE_TOKEN, interval, start, end, page_size=1000).candles
    )


def counts(report, interval: CandleInterval) -> tuple[int, int, int]:  # noqa: ANN001
    saved = report.saved_for(interval)
    return (saved.inserted, saved.identical, saved.conflict_count)


async def test_one_window_is_fetched_aggregated_and_stored(repository) -> None:  # noqa: ANN001
    script = Script({FRI: [session(FRI)]})

    report = await service(script, repository).ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)

    assert report.completed and report.windows_planned == 1
    assert [(w.outcome, w.bars_fetched) for w in report.windows] == [(WindowOutcome.INGESTED, 15)]
    assert (counts(report, M1), counts(report, M5), counts(report, M15)) == (
        (15, 0, 0),
        (3, 0, 0),
        (1, 0, 0),
    )
    assert (report.incomplete_slots, report.no_data_sessions) == ((), ())

    assert len(stored(repository, M1, FRI, SAT)) == 15
    first_5m, *_ = stored(repository, M5, FRI, SAT)
    (quarter,) = stored(repository, M15, FRI, SAT)
    ohlcv = lambda c: (c.open, c.high, c.low, c.close, c.volume)  # noqa: E731
    assert ohlcv(first_5m) == (100, 105, 99, Decimal("104.5"), 50)
    assert ohlcv(quarter) == (100, 115, 99, Decimal("114.5"), 150)
    assert (first_5m.start_at, first_5m.source) == (FRI + OPEN, "zerodha_historical")


async def test_a_missing_minute_is_reported_and_never_stored_as_a_partial_bar(repository) -> None:  # noqa: ANN001
    script = Script({FRI: [session(FRI, skip=(7,))]})

    report = await service(script, repository).ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)

    missing = FRI + OPEN + timedelta(minutes=7)  # 09:22
    assert report.incomplete_slots == (
        (M5, IncompleteSlot(FRI + OPEN + timedelta(minutes=5), (missing,))),
        (M15, IncompleteSlot(FRI + OPEN, (missing,))),
    )
    assert (counts(report, M1), counts(report, M5), counts(report, M15)) == (
        (14, 0, 0),
        (2, 0, 0),
        (0, 0, 0),
    )
    assert [c.start_at for c in stored(repository, M5, FRI, SAT)] == [
        FRI + OPEN,
        FRI + OPEN + timedelta(minutes=10),
    ]
    assert stored(repository, M15, FRI, SAT) == []


async def test_windows_run_in_order_and_an_empty_trading_day_is_no_data(repository) -> None:  # noqa: ANN001
    """Thursday has bars; Friday and Saturday answer with none. Both empty windows
    succeed as NO_DATA, but only Friday is a no-data session - Saturday is not a
    trading day on the calendar."""
    script = Script({THU: [session(THU)], FRI: [[]], SAT: [[]]})

    report = await service(script, repository).ingest(RELIANCE, start=THU, end=SUN, as_of=AS_OF)

    assert report.completed
    assert [(w.window, w.outcome) for w in report.windows] == [
        (RequestWindow(THU, FRI), WindowOutcome.INGESTED),
        (RequestWindow(FRI, SAT), WindowOutcome.NO_DATA),
        (RequestWindow(SAT, SUN), WindowOutcome.NO_DATA),
    ]
    assert report.no_data_sessions == (date(2026, 8, 21),)
    assert [e for e in script.events if e[0] == "fetch"] == [
        ("fetch", THU),
        ("fetch", FRI),
        ("fetch", SAT),
    ]
    assert report.bars_fetched == 15


async def test_each_attempt_takes_a_throttle_slot_and_retries_back_off(repository) -> None:  # noqa: ANN001
    script = Script({FRI: [classify_response(429, None, "slow down"), session(FRI)]})

    report = await service(script, repository).ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)

    assert report.completed
    assert script.events == [
        ("acquire",),
        ("fetch", FRI),
        ("sleep", 1.0),
        ("acquire",),
        ("fetch", FRI),
    ]


async def test_re_ingestion_writes_nothing_new_and_reports_identically(repository) -> None:  # noqa: ANN001
    script = Script({FRI: [session(FRI)]})
    ingest = service(script, repository).ingest

    await ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)
    second = await ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)
    third = await ingest(RELIANCE, start=FRI, end=SAT, as_of=AS_OF)

    assert (counts(second, M1), counts(second, M5), counts(second, M15)) == (
        (0, 15, 0),
        (0, 3, 0),
        (0, 1, 0),
    )
    assert second == third
    assert [len(stored(repository, i, FRI, SAT)) for i in (M1, M5, M15)] == [15, 3, 1]


async def test_a_changed_bar_is_a_conflict_and_the_stored_bar_stands(repository) -> None:  # noqa: ANN001
    original = session(FRI)
    await service(Script({FRI: [original]}), repository).ingest(
        RELIANCE, start=FRI, end=SAT, as_of=AS_OF
    )

    revised = list(original)
    revised[4] = replace(original[4], close=Decimal("104.75"))  # 09:19, last minute of a 5m slot
    report = await service(Script({FRI: [revised]}), repository).ingest(
        RELIANCE, start=FRI, end=SAT, as_of=AS_OF
    )

    # The 1m bar and the 09:15 5m bar it closes conflict; the 15m bar is unaffected.
    assert report.completed
    assert (counts(report, M1), counts(report, M5), counts(report, M15)) == (
        (0, 14, 1),
        (0, 2, 1),
        (0, 1, 0),
    )
    assert stored(repository, M1, FRI, SAT)[4].close == Decimal("104.5")
    assert stored(repository, M5, FRI, SAT)[0].close == Decimal("104.5")


async def test_exhausted_transient_failure_stops_the_run_and_keeps_earlier_windows(
    repository,
) -> None:  # noqa: ANN001
    script = Script(
        {THU: [session(THU)], FRI: [classify_response(429, None, "slow down")], SAT: [[]]}
    )

    report = await service(script, repository).ingest(RELIANCE, start=THU, end=SUN, as_of=AS_OF)

    assert not report.completed
    assert (report.windows_planned, report.windows_completed, report.windows_failed) == (3, 1, 1)
    failure = report.failure
    assert failure is not None
    assert failure.window == RequestWindow(FRI, SAT)
    assert failure.error is not None and failure.error.startswith("ZerodhaRateLimitedError:")
    assert [e for e in script.events if e[0] == "fetch"] == [
        ("fetch", THU),
        ("fetch", FRI),
        ("fetch", FRI),
    ]
    assert len(stored(repository, M1, THU, SUN)) == 15  # Thursday survives


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        pytest.param(
            classify_response(403, "TokenException", "expired"), ZerodhaAuthError, id="auth"
        ),
        pytest.param(
            classify_response(400, "InputException", "bad"), ZerodhaInputError, id="input"
        ),
        pytest.param(
            ZerodhaProtocolError("row 0 must have 6 fields"), ZerodhaProtocolError, id="protocol"
        ),
    ],
)
async def test_permanent_failures_abort_at_once_with_the_partial_report(
    repository,
    error: Exception,
    kind: type[Exception],  # noqa: ANN001
) -> None:
    script = Script({THU: [session(THU)], FRI: [error]})

    with pytest.raises(HistoricalIngestionAborted) as raised:
        await service(script, repository, max_attempts=4).ingest(
            RELIANCE, start=THU, end=SUN, as_of=AS_OF
        )

    assert isinstance(raised.value.__cause__, kind)
    report = raised.value.report
    assert [w.outcome for w in report.windows] == [WindowOutcome.INGESTED, WindowOutcome.FAILED]
    assert report.failure is not None and report.failure.window == RequestWindow(FRI, SAT)
    assert not report.completed
    assert [e for e in script.events if e[0] == "fetch"] == [("fetch", THU), ("fetch", FRI)]
    assert len(stored(repository, M1, THU, SUN)) == 15


async def test_a_range_off_the_fifteen_minute_grid_is_rejected_before_any_fetch(repository) -> None:  # noqa: ANN001
    script = Script({})
    with pytest.raises(ValueError, match="15-minute boundary"):
        await service(script, repository).ingest(
            RELIANCE, start=FRI + timedelta(minutes=5), end=SAT, as_of=AS_OF
        )
    assert script.events == []


async def test_end_to_end_through_the_real_client_makes_only_historical_gets(repository) -> None:  # noqa: ANN001
    """The service over the real Zerodha client and a mock transport: every
    request is a read-only historical GET for 1m data - nothing else is ever
    called - and the stored bars match the script-driven tests."""
    import httpx

    from app.adapters.zerodha.client import ZerodhaRestClient
    from app.domain.market.ports import HistoricalCandleSource

    friday_rows = ", ".join(
        f'["2026-08-21T09:{15 + i}:00+0530", {100 + i}, {101 + i}, {99 + i}, {100 + i}.5, 10]'
        for i in range(15)
    )
    bodies = {
        "2026-08-20 00:00:00": '{"status": "success", "data": {"candles": []}}',
        "2026-08-21 00:00:00": '{"status": "success", "data": {"candles": [' + friday_rows + "]}}",
    }
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=bodies[request.url.params["from"]])

    client = ZerodhaRestClient(
        api_key="k",
        access_token="t",
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    assert isinstance(client, HistoricalCandleSource)
    script = Script({})  # supplies fake time only
    ingestion = HistoricalIngestionService(
        source=client,
        repository=repository,
        throttle=RequestThrottle(script.clock, script.sleep),
        retry_policy=RetryPolicy(),
        sleep=script.sleep,
        max_window=DAY,
    )

    report = await ingestion.ingest(RELIANCE, start=THU, end=SAT, as_of=AS_OF)

    assert [(r.method, r.url.path, dict(r.url.params)) for r in requests] == [
        (
            "GET",
            "/instruments/historical/738561/minute",
            {"from": "2026-08-20 00:00:00", "to": "2026-08-21 00:00:00"},
        ),
        (
            "GET",
            "/instruments/historical/738561/minute",
            {"from": "2026-08-21 00:00:00", "to": "2026-08-22 00:00:00"},
        ),
    ]
    assert report.completed
    assert [w.outcome for w in report.windows] == [WindowOutcome.NO_DATA, WindowOutcome.INGESTED]
    assert report.no_data_sessions == (date(2026, 8, 20),)
    assert (counts(report, M1), counts(report, M5), counts(report, M15)) == (
        (15, 0, 0),
        (3, 0, 0),
        (1, 0, 0),
    )
    assert [c.close for c in stored(repository, M15, THU, SAT)] == [Decimal("114.5")]

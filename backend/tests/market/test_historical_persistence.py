"""Conflict-safe historical candle persistence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.ports import CandleSaveResult
from tests.market.conftest import RELIANCE_TOKEN

START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
M1 = CandleInterval.M1


def bar(minute: int, **overrides: object) -> Candle:
    start = START + timedelta(minutes=minute)
    values: dict[str, object] = {
        "instrument_token": RELIANCE_TOKEN,
        "interval": M1,
        "start_at": start,
        "end_at": start + M1.delta,
        "open": Decimal("1400.10"),
        "high": Decimal("1402.00"),
        "low": Decimal("1399.50"),
        "close": Decimal("1401.25"),
        "volume": 1200 + minute,
        "status": CandleStatus.COMPLETED,
        "source": "zerodha_historical",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
    }
    values.update(overrides)
    return Candle(**values)  # type: ignore[arg-type]


def stored(repository, count: int = 100) -> list[Candle]:  # noqa: ANN001
    return repository.candles_in_range(RELIANCE_TOKEN, M1, START, START + timedelta(minutes=count))


def facts(candle: Candle) -> tuple[object, ...]:
    return (
        candle.start_at,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
        candle.source,
    )


def test_new_bars_are_inserted(repository) -> None:  # noqa: ANN001
    bars = [bar(i) for i in range(3)]

    assert repository.save_historical_candles(bars) == CandleSaveResult(inserted=3, identical=0)
    assert [facts(c) for c in stored(repository)] == [facts(b) for b in bars]


def test_identical_re_ingestion_is_a_repeatable_no_op(repository) -> None:  # noqa: ANN001
    """Prices come back from NUMERIC(20, 6) as 1400.100000 and still match 1400.10."""
    bars = [bar(i) for i in range(3)]
    repository.save_historical_candles(bars)

    for _ in range(2):
        assert repository.save_historical_candles(bars) == CandleSaveResult(inserted=0, identical=3)
    assert len(stored(repository)) == 3


def test_changed_bars_are_conflicts_and_never_overwrite(repository) -> None:  # noqa: ANN001
    originals = [bar(i) for i in range(6)]
    repository.save_historical_candles(originals)

    changed = [
        replace(originals[1], open=Decimal("1400.15")),
        replace(originals[2], high=Decimal("1402.05")),
        replace(originals[3], low=Decimal("1399.45")),
        replace(originals[4], close=Decimal("1401.30")),
        replace(originals[5], volume=originals[5].volume + 1),
    ]
    incoming = [originals[0], *changed, bar(6)]

    result = repository.save_historical_candles(incoming)

    assert (result.inserted, result.identical, result.conflict_count) == (1, 1, 5)
    assert result.conflicts == tuple(changed)
    assert [facts(c) for c in stored(repository)] == [facts(b) for b in [*originals, bar(6)]]


def test_a_live_built_bar_is_not_replaced_by_a_historical_one(repository) -> None:  # noqa: ANN001
    live = bar(0, source="zerodha", tick_count=42)
    repository.save_candles([live])

    historical = bar(0)  # identical prices and volume, different source
    result = repository.save_historical_candles([historical])

    assert result == CandleSaveResult(inserted=0, identical=0, conflicts=(historical,))
    (kept,) = stored(repository)
    assert (kept.source, kept.tick_count) == ("zerodha", 42)


def test_duplicates_inside_one_batch_follow_the_same_rules(repository) -> None:  # noqa: ANN001
    first = bar(0)
    revised = replace(first, close=Decimal("1401.50"))

    result = repository.save_historical_candles([first, first, revised])

    assert result == CandleSaveResult(inserted=1, identical=1, conflicts=(revised,))
    assert [facts(c) for c in stored(repository)] == [facts(first)]


def test_an_unsettled_bar_is_refused_and_nothing_is_written(repository) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="not COMPLETED"):
        repository.save_historical_candles([bar(0), bar(1, status=CandleStatus.IN_PROGRESS)])
    assert stored(repository) == []


# --------------------------------------------------------------------------- #
# complete paginated range reads
# --------------------------------------------------------------------------- #


def read_all(repository, start: datetime, end: datetime, page_size: int) -> list[list[Candle]]:  # noqa: ANN001
    """Follow next_after to the end, returning each page."""
    pages: list[list[Candle]] = []
    after = None
    while True:
        page = repository.candles_page(
            RELIANCE_TOKEN, M1, start, end, page_size=page_size, after=after
        )
        pages.append(list(page.candles))
        if page.next_after is None:
            return pages
        after = page.next_after


def test_more_than_five_thousand_bars_page_back_complete_and_in_order(repository) -> None:  # noqa: ANN001
    count = 5_201
    repository.save_historical_candles([bar(i) for i in range(count)])
    end = START + timedelta(minutes=count)

    # The capped read this replaces for history stops short without saying so.
    assert len(repository.candles_in_range(RELIANCE_TOKEN, M1, START, end)) == 5_000

    pages = read_all(repository, START, end, page_size=2_000)

    assert [len(page) for page in pages] == [2_000, 2_000, 1_201]
    starts = [c.start_at for page in pages for c in page]
    # Every minute exactly once, ascending: no gap, no duplicate.
    assert starts == [START + timedelta(minutes=i) for i in range(count)]
    assert read_all(repository, START, end, page_size=2_000) == pages


def test_an_exactly_full_last_page_ends_the_read(repository) -> None:  # noqa: ANN001
    repository.save_historical_candles([bar(i) for i in range(4)])
    end = START + timedelta(minutes=4)

    first = repository.candles_page(RELIANCE_TOKEN, M1, START, end, page_size=2)
    second = repository.candles_page(
        RELIANCE_TOKEN, M1, START, end, page_size=2, after=first.next_after
    )

    assert (len(first.candles), first.next_after) == (2, START + timedelta(minutes=1))
    assert (len(second.candles), second.next_after) == (2, None)


def test_pages_respect_the_half_open_bounds_and_the_series_key(repository) -> None:  # noqa: ANN001
    five_minute = bar(0, interval=CandleInterval.M5, end_at=START + CandleInterval.M5.delta)
    other_instrument = bar(3, instrument_token=408065)
    repository.save_historical_candles(
        [*(bar(i) for i in range(10)), other_instrument, five_minute]
    )

    pages = read_all(
        repository, START + timedelta(minutes=2), START + timedelta(minutes=7), page_size=3
    )

    assert [[c.start_at for c in page] for page in pages] == [
        [START + timedelta(minutes=i) for i in (2, 3, 4)],
        [START + timedelta(minutes=i) for i in (5, 6)],
    ]
    assert all(c.instrument_token == RELIANCE_TOKEN and c.interval is M1 for p in pages for c in p)


@pytest.mark.parametrize(
    ("start", "end", "page_size"),
    [
        pytest.param(START, START + timedelta(minutes=5), 0, id="zero-page-size"),
        pytest.param(START, START, 10, id="empty-range"),
        pytest.param(START.replace(tzinfo=None), START + timedelta(minutes=5), 10, id="naive"),
    ],
)
def test_invalid_page_requests_are_rejected(
    repository,  # noqa: ANN001
    start: datetime,
    end: datetime,
    page_size: int,
) -> None:
    with pytest.raises(ValueError):
        repository.candles_page(RELIANCE_TOKEN, M1, start, end, page_size=page_size)

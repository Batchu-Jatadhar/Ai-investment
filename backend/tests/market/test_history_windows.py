"""Historical request window planning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.time import IST
from app.domain.market.windows import DEFAULT_MINUTE_WINDOW, RequestWindow, plan_windows

START = datetime(2026, 1, 1, 3, 45, tzinfo=UTC)
DAY = timedelta(days=1)


def test_a_range_shorter_than_the_limit_is_one_window() -> None:
    assert plan_windows(START, START + 5 * DAY) == (RequestWindow(START, START + 5 * DAY),)


def test_an_exact_multiple_of_the_limit_splits_on_the_boundary() -> None:
    assert plan_windows(START, START + 60 * DAY) == (
        RequestWindow(START, START + 30 * DAY),
        RequestWindow(START + 30 * DAY, START + 60 * DAY),
    )
    assert plan_windows(START, START + 30 * DAY) == (RequestWindow(START, START + 30 * DAY),)


@pytest.mark.parametrize(
    ("length", "max_span", "expected_spans"),
    [
        (65 * DAY, DEFAULT_MINUTE_WINDOW, [30 * DAY, 30 * DAY, 5 * DAY]),
        (20 * DAY, 7 * DAY, [7 * DAY, 7 * DAY, 6 * DAY]),
        (timedelta(days=90, hours=6), DEFAULT_MINUTE_WINDOW, [30 * DAY] * 3 + [timedelta(hours=6)]),
    ],
)
def test_a_longer_range_tiles_contiguously_with_a_shorter_final_window(
    length: timedelta, max_span: timedelta, expected_spans: list[timedelta]
) -> None:
    windows = plan_windows(START, START + length, max_span=max_span)

    assert [w.end - w.start for w in windows] == expected_spans
    assert windows[0].start == START
    assert windows[-1].end == START + length
    # No gap and no overlap: each window begins exactly where the last ended.
    assert all(a.end == b.start for a, b in zip(windows, windows[1:], strict=False))
    assert plan_windows(START, START + length, max_span=max_span) == windows


def test_inputs_are_normalised_to_utc() -> None:
    ist = START.astimezone(IST)
    (window,) = plan_windows(ist, ist + DAY)
    assert window == RequestWindow(START, START + DAY)
    assert window.start.tzinfo is UTC


@pytest.mark.parametrize(
    ("start", "end", "max_span"),
    [
        pytest.param(
            START.replace(tzinfo=None), START + DAY, DEFAULT_MINUTE_WINDOW, id="naive-start"
        ),
        pytest.param(
            START, (START + DAY).replace(tzinfo=None), DEFAULT_MINUTE_WINDOW, id="naive-end"
        ),
        pytest.param(START, START, DEFAULT_MINUTE_WINDOW, id="empty-range"),
        pytest.param(START + DAY, START, DEFAULT_MINUTE_WINDOW, id="inverted-range"),
        pytest.param(START, START + DAY, timedelta(0), id="zero-span"),
    ],
)
def test_invalid_ranges_are_rejected(start: datetime, end: datetime, max_span: timedelta) -> None:
    with pytest.raises(ValueError):
        plan_windows(start, end, max_span=max_span)

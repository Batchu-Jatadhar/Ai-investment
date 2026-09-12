"""Loading a BacktestInput from stored historical candles (SQL repository)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.time import IST
from app.domain.backtest.config import NSE_INTRADAY_EQUITY, ExecutionConfig, SlippageConfig
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.ports import CandlePage
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.params import OrbParams
from app.services.backtest_data import (
    HistoricalDataError,
    InsufficientWarmupError,
    load_backtest_input,
)
from tests.market.conftest import INFY_TOKEN, RELIANCE_TOKEN, make_instrument

M1, M5 = CandleInterval.M1, CandleInterval.M5
RELIANCE = make_instrument(RELIANCE_TOKEN, "RELIANCE")
OPEN = time(9, 15)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), IST).astimezone(UTC)


def session_minutes(day: date, count: int, token: int = RELIANCE_TOKEN) -> list[Candle]:
    first = datetime.combine(day, OPEN, IST).astimezone(UTC)
    bars = []
    for i in range(count):
        price = Decimal(1000 + i % 7)
        start = first + timedelta(minutes=i)
        bars.append(
            Candle(
                instrument_token=token,
                interval=M1,
                start_at=start,
                end_at=start + M1.delta,
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + 1,
                volume=100 + i,
                status=CandleStatus.COMPLETED,
                source="zerodha_historical",
            )
        )
    return bars


def store(repository, day: date, minutes: int, token: int = RELIANCE_TOKEN) -> None:  # noqa: ANN001
    bars = session_minutes(day, minutes, token)
    repository.save_historical_candles(bars)
    repository.save_historical_candles(aggregate_minutes(bars, M5).candles)


def load(repository, warmup: date, start: date, end: date, **kwargs: object):  # noqa: ANN001, ANN201
    return load_backtest_input(
        repository,
        RELIANCE,
        warmup_start=midnight(warmup),
        start=midnight(start),
        end=midnight(end),
        strategy_params=OrbParams(),
        cost_schedule=NSE_INTRADAY_EQUITY,
        slippage_config=SlippageConfig(),
        execution_config=ExecutionConfig(),
        calendar=MarketSessionCalendar.nse_equity(),
        **kwargs,  # type: ignore[arg-type]
    )


def edit_row(
    interval: CandleInterval, index: int, *, delete: bool = False, **changes: object
) -> None:
    """Change or delete the ``index``-th stored RELIANCE bar, bypassing the save rules."""
    from app.infrastructure.db import get_session_factory
    from app.infrastructure.models import CandleRecord

    with get_session_factory()() as session:
        row = session.execute(
            select(CandleRecord)
            .where(
                CandleRecord.instrument_token == RELIANCE_TOKEN,
                CandleRecord.interval == interval.value,
            )
            .order_by(CandleRecord.start_at)
            .offset(index)
            .limit(1)
        ).scalar_one()
        if delete:
            session.delete(row)
        for name, value in changes.items():
            setattr(row, name, value)
        session.commit()


MON, TUE, WED, THU, FRI = (date(2026, 8, day) for day in (3, 4, 5, 6, 7))


def test_a_long_range_loads_completely_through_pagination(repository) -> None:  # noqa: ANN001
    """14 full sessions: 5,250 minutes and 1,050 five-minute bars, read 1,000 at a time."""
    days = [date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 19, 20)]
    for day in days:
        store(repository, day, 375)
    store(repository, date(2026, 7, 31), 30)  # before the warmup
    store(repository, date(2026, 8, 21), 30)  # at the exclusive end
    store(repository, TUE, 375, token=INFY_TOKEN)  # another instrument

    loaded = load(repository, MON, TUE, date(2026, 8, 21), page_size=1_000)
    data = loaded.backtest_input

    assert (len(data.candles_1m), len(data.candles_5m)) == (5_250, 1_050)
    expected_minutes = [c.start_at for day in days for c in session_minutes(day, 375)]
    assert [c.start_at for c in data.candles_1m] == expected_minutes
    assert all(c.instrument_token == RELIANCE_TOKEN for c in (*data.candles_1m, *data.candles_5m))
    assert all(
        a.start_at < b.start_at for a, b in zip(data.candles_5m, data.candles_5m[1:], strict=False)
    )
    assert (loaded.warmup_sessions, loaded.trading_sessions) == ((MON,), tuple(days[1:]))
    assert loaded.sessions_without_data == ()
    assert data.prior_atr(TUE) is not None


def test_exact_window_warmup_split_and_sessions_without_data(repository) -> None:  # noqa: ANN001
    for day, minutes in ((MON, 80), (TUE, 80), (THU, 80), (FRI, 80)):
        store(repository, day, minutes)

    loaded = load(repository, MON, TUE, FRI)

    candles = loaded.backtest_input.candles_5m
    assert to_day(candles[0]) == MON and to_day(candles[-1]) == THU  # Friday is past the end
    assert (loaded.warmup_sessions, loaded.trading_sessions) == ((MON,), (TUE, THU))
    assert loaded.sessions_without_data == (WED,)
    assert (loaded.start, loaded.end) == (midnight(TUE), midnight(FRI))


def to_day(candle: Candle) -> date:
    return candle.start_at.astimezone(IST).date()


def test_the_fingerprint_follows_the_stored_data(repository) -> None:  # noqa: ANN001
    store(repository, MON, 80)
    store(repository, TUE, 80)

    first = load(repository, MON, TUE, WED).backtest_input.fingerprint()
    assert load(repository, MON, TUE, WED).backtest_input.fingerprint() == first

    # One minute's volume, and its five-minute bar's, changed consistently.
    edit_row(M1, 0, volume=101)
    edit_row(M5, 0, volume=sum(100 + i for i in range(5)) + 1)
    assert load(repository, MON, TUE, WED).backtest_input.fingerprint() != first


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            lambda: edit_row(M1, 2, delete=True), "incomplete 1m coverage", id="missing-minute"
        ),
        pytest.param(lambda: edit_row(M5, 3, high=Decimal(1100)), "disagrees", id="5m-disagrees"),
        pytest.param(
            lambda: edit_row(M5, 0, status="in_progress"), "not completed", id="in-progress"
        ),
        pytest.param(lambda: edit_row(M1, 5, source="zerodha"), "came from", id="live-source"),
    ],
)
def test_bad_stored_data_is_refused_not_repaired(repository, edit, message: str) -> None:  # noqa: ANN001
    store(repository, MON, 80)
    store(repository, TUE, 80)
    edit()

    with pytest.raises(HistoricalDataError, match=message):
        load(repository, MON, TUE, WED)


@pytest.mark.parametrize(
    ("sessions", "warmup", "error", "message"),
    [
        pytest.param(((MON, 10), (TUE, 80)), MON, InsufficientWarmupError, "holds 2", id="short"),
        pytest.param(((TUE, 80),), TUE, InsufficientWarmupError, "holds 0", id="none"),
        pytest.param(
            ((date(2026, 7, 31), 80), (MON, 80), (TUE, 80)),
            date(2026, 7, 31),
            HistoricalDataError,
            "would trade",
            id="warmup-would-trade",
        ),
        pytest.param(((MON, 80),), MON, HistoricalDataError, "backtest window", id="empty-window"),
    ],
)
def test_warmup_must_give_the_first_session_an_atr_and_nothing_else(
    repository,
    sessions,
    warmup: date,
    error: type[Exception],
    message: str,  # noqa: ANN001
) -> None:
    for day, minutes in sessions:
        store(repository, day, minutes)

    with pytest.raises(error, match=message):
        load(repository, warmup, TUE, WED)


class Tampered:
    """The real repository, with its pages corrupted on the way out."""

    def __init__(self, inner, mode: str) -> None:  # noqa: ANN001
        self.inner, self.mode = inner, mode

    def candles_page(self, *args, **kwargs) -> CandlePage:  # noqa: ANN002, ANN003
        page = self.inner.candles_page(*args, **kwargs)
        bars = page.candles
        bars = (bars[0], *bars) if self.mode == "duplicate" else (bars[1], bars[0], *bars[2:])
        return CandlePage(bars, page.next_after)


@pytest.mark.parametrize(
    ("mode", "message"), [("duplicate", "duplicates"), ("swap", "out of order")]
)
def test_duplicate_or_out_of_order_pages_are_refused(repository, mode: str, message: str) -> None:  # noqa: ANN001
    store(repository, MON, 80)
    store(repository, TUE, 80)

    with pytest.raises(HistoricalDataError, match=message):
        load(Tampered(repository, mode), MON, TUE, WED)


def test_bounds_must_be_ordered_ist_midnights(repository) -> None:  # noqa: ANN001
    with pytest.raises(HistoricalDataError, match="IST midnight"):
        load_backtest_input(
            repository,
            RELIANCE,
            warmup_start=midnight(MON),
            start=midnight(TUE) + timedelta(hours=9),
            end=midnight(WED),
            strategy_params=OrbParams(),
            cost_schedule=NSE_INTRADAY_EQUITY,
            slippage_config=SlippageConfig(),
            execution_config=ExecutionConfig(),
            calendar=MarketSessionCalendar.nse_equity(),
        )
    with pytest.raises(HistoricalDataError, match="warmup_start <= start < end"):
        load(repository, WED, TUE, THU)

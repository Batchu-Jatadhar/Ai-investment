"""Build a validated BacktestInput from stored historical candles.

Reads only what ingestion stored: no broker call, no clock. Every candle in the
range is read through :meth:`MarketDataRepository.candles_page`, following
``next_after`` to the end, so no row cap can shorten a run without saying so.

.. rubric:: Warmup and the backtest window

The strategy's range filter needs a prior ATR, which
:meth:`BacktestInput.prior_atr` derives from completed signal bars *before*
each session. So a run covers two spans, stated separately:

*   ``[warmup_start, start)`` - warmup. Loaded into the input so the first
    session of the window has an ATR, and it must not be traded itself.
*   ``[start, end)`` - the backtest window, the sessions the run is about.

The engine trades any session that has an ATR, so the loader requires that no
warmup session has one: in practice the warmup is the single session before
``start``. And it requires that the window's first session does have one. When
either fails it raises; it never shortens the window, invents an ATR or fetches
more history.

.. rubric:: What is refused

Bad data raises :class:`HistoricalDataError` rather than being repaired: a bar
that is not COMPLETED, from another source, out of order or duplicated; a
signal bar whose minutes are incomplete; a signal bar that disagrees with the
minutes it should aggregate. :class:`BacktestInput`'s own validation and
fingerprint then apply unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.core.time import ensure_utc, to_ist
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.input import BacktestInput
from app.domain.indicators import DEFAULT_ATR_PERIOD
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval, CandleStatus, Instrument
from app.domain.market.ports import MarketDataRepository
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.params import OrbParams

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "HISTORICAL_SOURCE",
    "HistoricalBacktestData",
    "HistoricalDataError",
    "InsufficientWarmupError",
    "load_backtest_input",
]

#: The provenance a loaded bar must carry. Mirrors the Zerodha adapter's label
#: without importing the adapter into a service that has no broker business.
HISTORICAL_SOURCE = "zerodha_historical"
DEFAULT_PAGE_SIZE = 10_000


class HistoricalDataError(ValueError):
    """Stored candles cannot back the requested run as they are."""


class InsufficientWarmupError(HistoricalDataError):
    """The window's first session has too little earlier history for its ATR."""


@dataclass(frozen=True, slots=True)
class HistoricalBacktestData:
    """A loaded input, and which of its sessions are warmup versus the window."""

    backtest_input: BacktestInput
    warmup_start: datetime
    start: datetime
    end: datetime
    warmup_sessions: tuple[date, ...]
    trading_sessions: tuple[date, ...]
    #: Calendar trading days in the window with no stored signal bars. Reported,
    #: not classified: nothing here knows whether the market was shut.
    sessions_without_data: tuple[date, ...]


def _require_ist_midnight(name: str, moment: datetime) -> datetime:
    utc = ensure_utc(moment)
    if to_ist(utc).time() != time(0):
        raise HistoricalDataError(
            f"{name} ({utc.isoformat()}) must be an IST midnight, so no session is split"
        )
    return utc


def _read_all(
    repository: MarketDataRepository,
    instrument: Instrument,
    interval: CandleInterval,
    start: datetime,
    end: datetime,
    page_size: int,
) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    after: datetime | None = None
    while True:
        page = repository.candles_page(
            instrument.instrument_token, interval, start, end, page_size=page_size, after=after
        )
        candles.extend(page.candles)
        if page.next_after is None:
            return tuple(candles)
        after = page.next_after


def _validate_series(
    candles: Sequence[Candle], instrument: Instrument, interval: CandleInterval, label: str
) -> None:
    if not candles:
        raise HistoricalDataError(f"no stored {interval.value} candles in the range ({label})")
    previous: datetime | None = None
    for candle in candles:
        where = f"{label} {interval.value} bar at {candle.start_at.isoformat()}"
        if candle.status is not CandleStatus.COMPLETED:
            raise HistoricalDataError(f"{where} is {candle.status.value}, not completed")
        if (
            candle.instrument_token != instrument.instrument_token
            or candle.interval is not interval
        ):
            raise HistoricalDataError(f"{where} belongs to another instrument or interval")
        if candle.source != HISTORICAL_SOURCE:
            raise HistoricalDataError(
                f"{where} came from {candle.source!r}, not {HISTORICAL_SOURCE!r}"
            )
        if candle.start_at.tzinfo is None:
            raise HistoricalDataError(f"{where} has a naive timestamp")
        if previous is not None and candle.start_at <= previous:
            problem = "duplicates" if candle.start_at == previous else "is out of order after"
            raise HistoricalDataError(f"{where} {problem} {previous.isoformat()}")
        previous = candle.start_at


def _validate_coverage(signal: Sequence[Candle], minutes: Sequence[Candle]) -> None:
    """Every signal bar must be exactly the aggregate of its own complete minutes."""
    if not signal:
        return
    built = {c.start_at: c for c in aggregate_minutes(minutes, signal[0].interval).candles}
    for bar in signal:
        expected = built.get(bar.start_at)
        if expected is None:
            raise HistoricalDataError(
                f"{bar.interval.value} bar at {bar.start_at.isoformat()} has incomplete 1m "
                "coverage; intrabar execution could not be resolved against it"
            )
        if (bar.open, bar.high, bar.low, bar.close, bar.volume) != (
            expected.open,
            expected.high,
            expected.low,
            expected.close,
            expected.volume,
        ):
            raise HistoricalDataError(
                f"{bar.interval.value} bar at {bar.start_at.isoformat()} disagrees with the "
                "1m bars it should aggregate"
            )


def load_backtest_input(
    repository: MarketDataRepository,
    instrument: Instrument,
    *,
    warmup_start: datetime,
    start: datetime,
    end: datetime,
    strategy_params: OrbParams,
    cost_schedule: CostSchedule,
    slippage_config: SlippageConfig,
    execution_config: ExecutionConfig,
    calendar: MarketSessionCalendar,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> HistoricalBacktestData:
    """Load ``[warmup_start, end)`` into a BacktestInput. See the module docstring.

    ``warmup_start``, ``start`` and ``end`` must be IST midnights with
    ``warmup_start <= start < end``.
    """
    warmup_start = _require_ist_midnight("warmup_start", warmup_start)
    start = _require_ist_midnight("start", start)
    end = _require_ist_midnight("end", end)
    if not warmup_start <= start < end:
        raise HistoricalDataError("expected warmup_start <= start < end")

    signal_interval = strategy_params.signal_interval
    resolution_interval = strategy_params.resolution_interval
    signal = _read_all(repository, instrument, signal_interval, warmup_start, end, page_size)
    minutes = _read_all(repository, instrument, resolution_interval, warmup_start, end, page_size)
    _validate_series(signal, instrument, signal_interval, "signal")
    _validate_series(minutes, instrument, resolution_interval, "resolution")
    _validate_coverage(signal, minutes)

    first_day = to_ist(start).date()
    sessions = sorted({to_ist(c.start_at).date() for c in signal})
    warmup_sessions = tuple(day for day in sessions if day < first_day)
    trading_sessions = tuple(day for day in sessions if day >= first_day)
    if not trading_sessions:
        raise HistoricalDataError(
            f"no stored {signal_interval.value} candles in the backtest window "
            f"{start.isoformat()} - {end.isoformat()}"
        )

    backtest_input = BacktestInput(
        instrument=instrument,
        candles_5m=signal,
        candles_1m=minutes,
        calendar=calendar,
        strategy_params=strategy_params,
        cost_schedule=cost_schedule,
        slippage_config=slippage_config,
        execution_config=execution_config,
    )

    first = trading_sessions[0]
    if backtest_input.prior_atr(first) is None:
        available = sum(1 for c in signal if to_ist(c.start_at).date() < first)
        raise InsufficientWarmupError(
            f"the first backtest session {first.isoformat()} needs at least "
            f"{DEFAULT_ATR_PERIOD + 1} completed {signal_interval.value} bars before it for its "
            f"ATR, but the warmup from {warmup_start.isoformat()} holds {available}; "
            "load an earlier warmup_start"
        )
    tradable_warmup = [day for day in warmup_sessions if backtest_input.prior_atr(day) is not None]
    if tradable_warmup:
        raise HistoricalDataError(
            f"warmup session {tradable_warmup[0].isoformat()} already has an ATR, so the engine "
            "would trade it; start the warmup at the single session before the window"
        )

    days = (to_ist(end).date() - first_day).days
    window_days = (first_day + timedelta(days=offset) for offset in range(days))
    return HistoricalBacktestData(
        backtest_input=backtest_input,
        warmup_start=warmup_start,
        start=start,
        end=end,
        warmup_sessions=warmup_sessions,
        trading_sessions=trading_sessions,
        sessions_without_data=tuple(
            day
            for day in window_days
            if calendar.is_trading_day(day) and day not in trading_sessions
        ),
    )

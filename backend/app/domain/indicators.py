"""Pure indicator calculations.

Everything here is a function of the bars it is handed and nothing else: no
clock, no repository, no provider, no configuration. An indicator that could
reach any of those would make a backtest unreproducible, and the same value
would be unavailable to a live session without a second implementation.

Two properties are load-bearing and are enforced rather than documented:

*   **No future bar is visible.** Each function takes an explicitly bounded
    sequence. The caller decides where the prefix ends; the indicator cannot
    look past it, because it is never given anything past it.
*   **Arithmetic is exact and context-independent.** Prices are ``Decimal`` and
    every division runs inside a fixed decimal context, so a caller that has
    changed the process-wide precision cannot change an indicator's result.

Only what the approved ORB hypothesis needs lives here. Indicators are added
when a strategy actually calls for one, not in anticipation of one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.core.time import ist_datetime, to_ist
from app.domain.market.models import Candle, CandleStatus
from app.domain.market.session import MarketSessionCalendar

__all__ = [
    "DEFAULT_ATR_PERIOD",
    "IndicatorError",
    "OpeningRange",
    "average_true_range",
    "opening_range",
    "true_range",
]

#: Wilder's original period, and the default on every charting platform. Kept
#: fixed rather than exposed as a tunable: it is part of the initial fixed
#: hypothesis, not something to search over.
DEFAULT_ATR_PERIOD = 14

#: Divisions run here rather than in whatever context the caller happens to
#: have installed. ``Decimal`` precision is process-global and mutable, so an
#: unpinned context would let an unrelated module change an indicator's value.
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class IndicatorError(ValueError):
    """Raised when a bar sequence cannot support the requested indicator.

    A ``ValueError`` subclass, following ``InvalidBacktestInputError``, so a
    caller can catch it precisely rather than matching on message text.

    Insufficient or malformed input always raises. Returning zero, ``None`` or a
    partial-window value would be worse than failing: a filter that compares an
    opening range against a silently degraded ATR would keep trading and the
    defect would surface only as unexplained results.
    """


def _validate_bars(candles: Sequence[Candle], label: str) -> None:
    """Reject a sequence that could not be a real, ordered run of one instrument."""
    if not candles:
        raise IndicatorError(f"{label} is empty")

    first = candles[0]
    previous: Candle | None = None
    for index, candle in enumerate(candles):
        if candle.status is not CandleStatus.COMPLETED:
            raise IndicatorError(
                f"{label}[{index}] at {candle.start_at.isoformat()} is {candle.status.value}; "
                "only completed bars may feed an indicator, because an in-progress bar still "
                "changes and the value computed from it would not settle until later"
            )
        if candle.instrument_token != first.instrument_token:
            raise IndicatorError(
                f"{label}[{index}] belongs to instrument {candle.instrument_token}, but the "
                f"sequence starts with {first.instrument_token}; an indicator covers one "
                "instrument"
            )
        if candle.interval is not first.interval:
            raise IndicatorError(
                f"{label}[{index}] has interval {candle.interval.value}, but the sequence "
                f"starts with {first.interval.value}; mixing intervals would average bars that "
                "cover different amounts of time"
            )
        if previous is not None and candle.start_at <= previous.start_at:
            raise IndicatorError(
                f"{label} is not strictly ascending: {candle.start_at.isoformat()} follows "
                f"{previous.start_at.isoformat()}"
            )
        previous = candle


def true_range(candle: Candle, previous_close: Decimal) -> Decimal:
    """Wilder's true range for one bar.

    The two gap terms are what distinguish this from the bar's own high-low
    range: when a bar opens away from the previous close, the distance actually
    travelled includes the gap, and a plain high-low would understate the day's
    movement exactly on the days that matter most.
    """
    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def average_true_range(candles: Sequence[Candle], period: int = DEFAULT_ATR_PERIOD) -> Decimal:
    """ATR over ``candles``, using Wilder's smoothing.

    **The convention, stated exactly, because ATR is ambiguous in the wild.**
    True range is computed for every bar after the first, since the first bar
    has no previous close to gap from. The first ``period`` true ranges are
    averaged arithmetically to seed the series, and each later bar is folded in
    with Wilder's recurrence::

        ATR = (previous_ATR * (period - 1) + true_range) / period

    That is the original 1978 definition and what every charting platform means
    by "ATR". No alternative smoothing is offered: a second variant would let
    the same parameter mean two different things in two places, and the
    hypothesis' ``max_range_atr_multiple`` names one of them.

    ``candles`` must hold at least ``period + 1`` bars, in ascending order, all
    completed, all one instrument, all one interval. The extra bar is not
    optional padding - it is the one that supplies the first previous close.

    Only the bars supplied are read, so passing a session prefix yields the
    value that was knowable at the end of that prefix.
    """
    if period < 1:
        raise IndicatorError(f"period must be at least 1, got {period}")

    _validate_bars(candles, "candles")
    if len(candles) < period + 1:
        raise IndicatorError(
            f"ATR({period}) needs at least {period + 1} bars but got {len(candles)}: "
            f"{period} true ranges to seed the average, plus one earlier bar to supply the "
            "first previous close"
        )

    ranges = [true_range(candle, candles[index].close) for index, candle in enumerate(candles[1:])]

    with localcontext(_CONTEXT):
        divisor = Decimal(period)
        atr = sum(ranges[:period], Decimal(0)) / divisor
        for value in ranges[period:]:
            atr = (atr * Decimal(period - 1) + value) / divisor
        return +atr


@dataclass(frozen=True, slots=True)
class OpeningRange:
    """The high and low of a session's opening window.

    Deliberately just the measurement. Whether the range is wide enough to
    trade, whether price has broken it, and where a stop would sit are all
    strategy decisions and none of them live here - an indicator that already
    knew the entry rule could not be reused by a different one, and could not be
    checked against a chart independently of the strategy it feeds.
    """

    session_date: date
    start_at: datetime
    end_at: datetime
    high: Decimal
    low: Decimal
    bar_count: int

    @property
    def width(self) -> Decimal:
        """High minus low. The unit of risk the ORB hypothesis is built on."""
        return self.high - self.low


def opening_range(
    candles: Sequence[Candle],
    session_date: date,
    calendar: MarketSessionCalendar,
    *,
    opening_range_minutes: int = 15,
) -> OpeningRange:
    """High and low across the opening window of ``session_date``.

    For the approved hypothesis that window is 09:15-09:30 IST, which is exactly
    three completed 5-minute bars. NSE runs a 09:00-09:15 pre-open call auction,
    so the window starts at an auction-cleared price rather than mid-discovery.

    The window is derived from ``calendar``, never from a wall clock: the same
    call for the same session date returns the same window on any machine at any
    moment, which is what lets a backtest and a live session agree.

    ``candles`` may hold any span - a whole session, or several. Only bars inside
    the window are read, so handing this a full day cannot leak an afternoon bar
    into the morning's range.

    Raises :class:`IndicatorError` rather than returning a partial range when the
    window is not fully covered. A range measured from two of its three bars is
    not a narrower range, it is a different and wrong number, and every stop and
    target derived from it downstream would inherit the error silently.
    """
    if opening_range_minutes <= 0:
        raise IndicatorError(f"opening_range_minutes must be positive, got {opening_range_minutes}")

    _validate_bars(candles, "candles")
    interval = candles[0].interval

    if (opening_range_minutes * 60) % interval.seconds != 0:
        raise IndicatorError(
            f"a {opening_range_minutes}-minute opening range is not a whole number of "
            f"{interval.value} bars; the window would end mid-bar and its high and low would "
            "depend on data the strategy could not yet have seen"
        )

    bounds = calendar.session_bounds(ist_datetime(session_date, calendar.window.open_time))
    if bounds is None:
        raise IndicatorError(
            f"{session_date.isoformat()} is not a trading day on the "
            f"{calendar.window.name} calendar, so it has no opening range"
        )
    window_start, _ = bounds
    window_end = window_start + timedelta(minutes=opening_range_minutes)

    if int(window_start.timestamp()) % interval.seconds != 0:
        raise IndicatorError(
            f"the {calendar.window.name} session starts at "
            f"{calendar.window.open_time.isoformat()} IST, which is not aligned to a "
            f"{interval.value} boundary; bar boundaries are epoch-aligned, so no bar begins "
            "when this session does"
        )

    expected = tuple(
        window_start + interval.delta * step
        for step in range((opening_range_minutes * 60) // interval.seconds)
    )
    window_bars = tuple(
        candle for candle in candles if window_start <= candle.start_at < window_end
    )

    if tuple(candle.start_at for candle in window_bars) != expected:
        present = {candle.start_at for candle in window_bars}
        missing = [moment for moment in expected if moment not in present]
        detail = (
            "missing " + ", ".join(to_ist(moment).time().isoformat() for moment in missing) + " IST"
            if missing
            else "its bars are not on the expected boundaries"
        )
        raise IndicatorError(
            f"the {opening_range_minutes}-minute opening range for "
            f"{session_date.isoformat()} needs {len(expected)} {interval.value} bars but "
            f"{detail}; the range cannot be measured from an incomplete window"
        )

    return OpeningRange(
        session_date=session_date,
        start_at=window_start,
        end_at=window_end,
        high=max(candle.high for candle in window_bars),
        low=min(candle.low for candle in window_bars),
        bar_count=len(window_bars),
    )

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
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.domain.market.models import Candle, CandleStatus

__all__ = [
    "DEFAULT_ATR_PERIOD",
    "IndicatorError",
    "average_true_range",
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

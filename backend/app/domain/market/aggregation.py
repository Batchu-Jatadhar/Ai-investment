"""Strict aggregation of completed 1-minute candles into 5m and 15m candles.

A pure function over bars already in hand, for data that arrives as a finished
series - historical candles - rather than tick by tick. The live
:class:`~app.domain.market.candles.CandleEngine` keeps its own streaming
roll-up; this does not replace it.

**A target bar exists only if every one of its minutes does.** A 5m bar needs
all five 1m buckets and a 15m bar all fifteen. A slot with a minute missing
produces no bar and is reported instead, naming the missing buckets. Nothing is
filled, interpolated or built from a partial window: a 5m bar made from four
minutes has a high, low and volume that describe a different five minutes than
the ones it claims, and every stop and target resolved against it would inherit
that silently.

Slots are epoch-aligned through :func:`~app.domain.market.candles.bucket_start`,
the same boundaries the live engine uses, so the 09:15 IST session open is a
slot boundary for both intervals.

Only slots that at least one input minute falls into are considered. A slot with
no minutes at all - a holiday, a halt, a range outside the data - is not
reported, because nothing here knows whether a market was open then.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby

from app.domain.market.candles import bucket_start
from app.domain.market.models import Candle, CandleInterval, CandleStatus

__all__ = [
    "AggregationResult",
    "IncompleteSlot",
    "InvalidMinuteSeriesError",
    "aggregate_minutes",
]

_TARGETS = (CandleInterval.M5, CandleInterval.M15)


class InvalidMinuteSeriesError(ValueError):
    """The source minutes could not be a real, ordered 1m series of one instrument.

    A ``ValueError`` subclass, following ``IndicatorError``, so a caller can
    catch it precisely rather than matching on message text.
    """


@dataclass(frozen=True, slots=True)
class IncompleteSlot:
    """A target slot that had some minutes but not all, so produced no bar."""

    start_at: datetime
    missing: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class AggregationResult:
    """The bars that could be built, and the slots that could not."""

    candles: tuple[Candle, ...]
    incomplete: tuple[IncompleteSlot, ...]


def _validate(minutes: Sequence[Candle]) -> None:
    first = minutes[0]
    previous: Candle | None = None
    for index, minute in enumerate(minutes):
        where = f"minutes[{index}] at {minute.start_at.isoformat()}"
        if minute.status is not CandleStatus.COMPLETED:
            raise InvalidMinuteSeriesError(
                f"{where} is {minute.status.value}; only completed minutes may be aggregated"
            )
        if minute.interval is not CandleInterval.M1:
            raise InvalidMinuteSeriesError(f"{where} is a {minute.interval.value} bar, not 1m")
        if minute.start_at != bucket_start(minute.start_at, CandleInterval.M1) or (
            minute.end_at != minute.start_at + CandleInterval.M1.delta
        ):
            raise InvalidMinuteSeriesError(f"{where} is not an aligned one-minute bucket")
        if minute.instrument_token != first.instrument_token:
            raise InvalidMinuteSeriesError(
                f"{where} belongs to instrument {minute.instrument_token}, not "
                f"{first.instrument_token}; one series covers one instrument"
            )
        if minute.source != first.source:
            raise InvalidMinuteSeriesError(
                f"{where} came from {minute.source!r}, not {first.source!r}; a bar built from "
                "two sources would have no single provenance"
            )
        if previous is not None and minute.start_at <= previous.start_at:
            problem = "duplicates" if minute.start_at == previous.start_at else "precedes"
            raise InvalidMinuteSeriesError(
                f"{where} {problem} the minute before it; the series must be strictly ascending"
            )
        previous = minute


def aggregate_minutes(minutes: Sequence[Candle], interval: CandleInterval) -> AggregationResult:
    """Aggregate completed, ascending 1m bars of one instrument into ``interval``.

    open is the first minute's open, high the highest high, low the lowest low,
    close the last minute's close, volume and tick count the sums. Timestamps
    stay UTC; instrument, symbol, exchange and source carry over from the
    minutes, and the result is COMPLETED.

    Raises :class:`InvalidMinuteSeriesError` for an unsupported ``interval`` or
    an invalid series. An empty series aggregates to an empty result.
    """
    if interval not in _TARGETS:
        raise InvalidMinuteSeriesError(
            f"cannot aggregate minutes into {interval.value}; supported: "
            f"{[target.value for target in _TARGETS]}"
        )
    if not minutes:
        return AggregationResult((), ())
    _validate(minutes)

    width = interval.seconds // CandleInterval.M1.seconds
    candles: list[Candle] = []
    incomplete: list[IncompleteSlot] = []

    for slot, grouped in groupby(minutes, key=lambda m: bucket_start(m.start_at, interval)):
        present = tuple(grouped)
        if len(present) < width:
            have = {m.start_at for m in present}
            expected = (slot + CandleInterval.M1.delta * step for step in range(width))
            incomplete.append(IncompleteSlot(slot, tuple(t for t in expected if t not in have)))
            continue

        first, last = present[0], present[-1]
        candles.append(
            Candle(
                instrument_token=first.instrument_token,
                interval=interval,
                start_at=slot,
                end_at=slot + interval.delta,
                open=first.open,
                high=max(m.high for m in present),
                low=min(m.low for m in present),
                close=last.close,
                volume=sum(m.volume for m in present),
                status=CandleStatus.COMPLETED,
                tick_count=sum(m.tick_count for m in present),
                source=first.source,
                tradingsymbol=first.tradingsymbol,
                exchange=first.exchange,
                last_update_at=last.last_update_at,
            )
        )

    return AggregationResult(tuple(candles), tuple(incomplete))

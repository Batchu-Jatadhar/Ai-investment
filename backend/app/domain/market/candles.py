"""Candle aggregation.

Ticks -> 1m bars -> 5m bars and 15m bars.

Two rules from the architecture are enforced here:

*   **Larger intervals aggregate completed 1-minute bars.** They are never
    derived independently from ticks. Two derivations of the same bar disagree
    at the edges, and that disagreement is invisible until a backtest cannot be
    reproduced.
*   **A bar is IN_PROGRESS until its window has elapsed.** Only COMPLETED bars
    are safe for downstream logic. This engine transforms ticks into bars and
    knows nothing about strategies.

Boundary alignment is on the UTC epoch. Because IST is UTC+05:30 and 30 is a
multiple of 1, 5 and 15, epoch-aligned boundaries coincide exactly with IST
clock boundaries and therefore with the 09:15 IST session open.

Volume: the feed reports *cumulative* day volume, so a bar's volume is the
increase across the bar. The first bar built after joining a live session has
no earlier baseline, so it is marked with ``volume_baseline_missing`` and its
volume counts only the trades observed after the first tick.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.core.time import ensure_utc
from app.domain.market.models import (
    Candle,
    CandleInterval,
    CandleStatus,
    MarketTick,
    _CandleAccumulator,
)

__all__ = ["CandleEngine", "bucket_start"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(moment: datetime, interval: CandleInterval) -> datetime:
    """Start of the interval bucket containing ``moment``."""
    aware = ensure_utc(moment)
    elapsed = int((aware - _EPOCH).total_seconds())
    return _EPOCH.replace(microsecond=0) + (interval.delta * (elapsed // interval.seconds))


class CandleEngine:
    """Builds bars for one or more instruments across several intervals."""

    def __init__(
        self,
        intervals: Sequence[CandleInterval] = (
            CandleInterval.M1,
            CandleInterval.M5,
            CandleInterval.M15,
        ),
        *,
        source: str = "unknown",
    ) -> None:
        if CandleInterval.M1 not in intervals:
            raise ValueError(
                "the 1-minute interval is required: larger intervals are built "
                "from completed 1-minute bars, not from ticks"
            )
        self._intervals = tuple(intervals)
        self._derived = tuple(i for i in self._intervals if i is not CandleInterval.M1)
        self._source = source
        # (token, interval) -> accumulator
        self._open: dict[tuple[int, CandleInterval], _CandleAccumulator] = {}
        # token -> cumulative volume at the close of the last completed 1m bar
        self._volume_carry: dict[int, int] = {}
        self.completed_count = 0

    # ------------------------------------------------------------------ #
    # properties
    # ------------------------------------------------------------------ #

    @property
    def intervals(self) -> tuple[CandleInterval, ...]:
        return self._intervals

    @property
    def open_bar_count(self) -> int:
        return len(self._open)

    def partial(self, instrument_token: int, interval: CandleInterval) -> Candle | None:
        """The current IN_PROGRESS bar, if one is open."""
        acc = self._open.get((instrument_token, interval))
        return acc.snapshot(CandleStatus.IN_PROGRESS) if acc else None

    def partials(self) -> list[Candle]:
        return [acc.snapshot(CandleStatus.IN_PROGRESS) for acc in self._open.values()]

    # ------------------------------------------------------------------ #
    # ingestion
    # ------------------------------------------------------------------ #

    def on_tick(self, tick: MarketTick) -> list[Candle]:
        """Feed one validated tick. Returns bars completed by this tick."""
        completed: list[Candle] = []
        start = bucket_start(tick.event_time, CandleInterval.M1)
        key = (tick.instrument_token, CandleInterval.M1)
        acc = self._open.get(key)

        if acc is not None and start > acc.start_at:
            completed.extend(self._close(key))
            acc = None
        elif acc is not None and start < acc.start_at:
            # Ordering is the validator's responsibility; if one slips through,
            # drop it rather than corrupting a bar that is already closed.
            return completed

        if acc is None:
            acc = self._open_bar(tick, start)

        self._apply_tick(acc, tick)
        return completed

    def on_ticks(self, ticks: Iterable[MarketTick]) -> list[Candle]:
        completed: list[Candle] = []
        for tick in ticks:
            completed.extend(self.on_tick(tick))
        return completed

    def flush(self, now: datetime) -> list[Candle]:
        """Complete every bar whose window has fully elapsed.

        Required because a bar cannot be completed by a tick that never comes:
        an illiquid instrument, the end of a session, or a stalled feed.
        """
        moment = ensure_utc(now)
        completed: list[Candle] = []
        # 1m first, so its completions can roll into the derived intervals.
        for interval in (CandleInterval.M1, *self._derived):
            for key in [
                k for k, acc in self._open.items() if k[1] is interval and acc.end_at <= moment
            ]:
                completed.extend(self._close(key))
        return completed

    def close_all(self) -> list[Candle]:
        """Complete every open bar regardless of the clock (session end, shutdown)."""
        completed: list[Candle] = []
        for interval in (CandleInterval.M1, *self._derived):
            for key in [k for k in list(self._open) if k[1] is interval]:
                completed.extend(self._close(key))
        return completed

    def reset(self) -> None:
        """Discard all open bars and volume baselines (used after a data gap)."""
        self._open.clear()
        self._volume_carry.clear()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _open_bar(self, tick: MarketTick, start: datetime) -> _CandleAccumulator:
        acc = _CandleAccumulator(
            instrument_token=tick.instrument_token,
            interval=CandleInterval.M1,
            start_at=start,
            end_at=start + CandleInterval.M1.delta,
            open=tick.last_price,
            high=tick.last_price,
            low=tick.last_price,
            close=tick.last_price,
            source=self._source,
            tradingsymbol=tick.tradingsymbol,
            exchange=tick.exchange,
        )
        acc._volume_baseline = self._volume_carry.get(tick.instrument_token)
        if acc._volume_baseline is None and tick.volume is not None:
            # No earlier bar to measure from - see the module docstring.
            acc._volume_baseline = tick.volume
        self._open[(tick.instrument_token, CandleInterval.M1)] = acc
        return acc

    def _apply_tick(self, acc: _CandleAccumulator, tick: MarketTick) -> None:
        price = tick.last_price
        acc.high = max(acc.high, price)
        acc.low = min(acc.low, price)
        acc.close = price
        acc.tick_count += 1
        acc.last_update_at = tick.event_time
        if tick.tradingsymbol and not acc.tradingsymbol:
            acc.tradingsymbol = tick.tradingsymbol
        if tick.exchange and not acc.exchange:
            acc.exchange = tick.exchange
        if tick.volume is not None and acc._volume_baseline is not None:
            acc.volume = max(0, tick.volume - acc._volume_baseline)
            self._volume_carry[tick.instrument_token] = tick.volume

    def _close(self, key: tuple[int, CandleInterval]) -> list[Candle]:
        acc = self._open.pop(key, None)
        if acc is None:
            return []
        candle = acc.snapshot(CandleStatus.COMPLETED)
        self.completed_count += 1
        results = [candle]
        if candle.interval is CandleInterval.M1:
            results.extend(self._roll_up(candle))
        return results

    def _roll_up(self, minute: Candle) -> list[Candle]:
        """Fold a completed 1-minute bar into every derived interval."""
        completed: list[Candle] = []
        for interval in self._derived:
            start = bucket_start(minute.start_at, interval)
            key = (minute.instrument_token, interval)
            acc = self._open.get(key)

            if acc is not None and start > acc.start_at:
                completed.extend(self._close(key))
                acc = None
            elif acc is not None and start < acc.start_at:
                continue

            if acc is None:
                acc = _CandleAccumulator(
                    instrument_token=minute.instrument_token,
                    interval=interval,
                    start_at=start,
                    end_at=start + interval.delta,
                    open=minute.open,
                    high=minute.high,
                    low=minute.low,
                    close=minute.close,
                    volume=0,
                    source=self._source,
                    tradingsymbol=minute.tradingsymbol,
                    exchange=minute.exchange,
                )
                self._open[key] = acc
            else:
                acc.high = max(acc.high, minute.high)
                acc.low = min(acc.low, minute.low)
                acc.close = minute.close

            acc.volume += minute.volume
            acc.tick_count += minute.tick_count
            acc.last_update_at = minute.last_update_at

            # A derived bar whose final minute has arrived is done immediately;
            # waiting for the next minute would delay it by a whole bar.
            if minute.end_at >= acc.end_at:
                completed.extend(self._close(key))
        return completed

    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, object]:
        """Observability snapshot."""
        return {
            "intervals": [i.value for i in self._intervals],
            "open_bars": self.open_bar_count,
            "completed_bars": self.completed_count,
            "instruments": len({token for token, _ in self._open}),
        }


def ohlc_from_prices(prices: Sequence[Decimal]) -> tuple[Decimal, ...]:  # pragma: no cover
    """Small helper used by tests and fixtures."""
    return (prices[0], max(prices), min(prices), prices[-1])

"""Historical candle ingestion for one instrument over one date range.

Read-only toward the broker. The workflow joins pieces that each already do one
thing and are tested on their own:

    plan_windows -> per window:
        throttle slot -> fetch 1m (retried on transient failure)
        -> save 1m -> aggregate 5m/15m from those same minutes -> save them

5m and 15m bars are never fetched. They are built locally from the fetched
minutes by :func:`~app.domain.market.aggregation.aggregate_minutes`, so the
1m and 5m series agree by construction and a missing minute leaves a reported
incomplete slot rather than a partial bar.

.. rubric:: When a window fails

Windows already ingested stay stored; saves are idempotent, so re-running the
same range later is safe.

*   A transient failure that exhausts its retries (429, network, 5xx) ends the
    run. The failed window is recorded and the report is returned with
    ``completed`` false.
*   Anything else from the fetch - an expired token, a rejected request, a
    malformed response - raises :class:`HistoricalIngestionAborted` carrying the
    partial report. Retrying those, or carrying on to the next window, would
    only repeat the same failure.

Conflicts are not failures. A bar that differs from the stored one is refused
by the repository, counted and returned; the run continues.

.. rubric:: Time

Nothing here reads a clock. ``start``, ``end`` and ``as_of`` are the caller's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from app.adapters.zerodha.errors import ZerodhaError
from app.adapters.zerodha.pacing import (
    RETRYABLE_ERRORS,
    RequestThrottle,
    RetryPolicy,
    Sleep,
    with_retries,
)
from app.core.logging import get_logger
from app.core.time import ensure_utc, to_ist
from app.domain.market.aggregation import IncompleteSlot, aggregate_minutes
from app.domain.market.candles import bucket_start
from app.domain.market.models import Candle, CandleInterval, Instrument
from app.domain.market.ports import (
    CandleSaveResult,
    HistoricalCandleSource,
    MarketDataRepository,
)
from app.domain.market.session import MarketSessionCalendar
from app.domain.market.windows import DEFAULT_MINUTE_WINDOW, RequestWindow, plan_windows

logger = get_logger(__name__)

__all__ = [
    "HistoricalIngestionAborted",
    "HistoricalIngestionReport",
    "HistoricalIngestionService",
    "WindowOutcome",
    "WindowReport",
]

_AGGREGATES = (CandleInterval.M5, CandleInterval.M15)


class WindowOutcome(StrEnum):
    INGESTED = "ingested"
    #: The request succeeded and returned no bars. Not a failure, and not
    #: labelled a holiday: nothing here knows why there was no data.
    NO_DATA = "no_data"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WindowReport:
    window: RequestWindow
    outcome: WindowOutcome
    bars_fetched: int = 0
    #: ``"<ErrorType>: <detail>"`` for a failed window.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HistoricalIngestionReport:
    """What one ingestion run did. Immutable; returned, not persisted."""

    instrument: Instrument
    start: datetime
    end: datetime
    as_of: datetime
    windows_planned: int
    #: Every window attempted, in order. Windows after a failure are absent.
    windows: tuple[WindowReport, ...]
    #: Save counts per interval, 1m first.
    saved: tuple[tuple[CandleInterval, CandleSaveResult], ...]
    incomplete_slots: tuple[tuple[CandleInterval, IncompleteSlot], ...]
    #: Trading days (by the calendar) inside successfully fetched windows that
    #: had no bars at all.
    no_data_sessions: tuple[date, ...]

    @property
    def windows_completed(self) -> int:
        return sum(1 for w in self.windows if w.outcome is not WindowOutcome.FAILED)

    @property
    def windows_failed(self) -> int:
        return sum(1 for w in self.windows if w.outcome is WindowOutcome.FAILED)

    @property
    def bars_fetched(self) -> int:
        return sum(w.bars_fetched for w in self.windows)

    @property
    def failure(self) -> WindowReport | None:
        return next((w for w in self.windows if w.outcome is WindowOutcome.FAILED), None)

    @property
    def completed(self) -> bool:
        """Every planned window was fetched and stored."""
        return self.windows_completed == self.windows_planned

    def saved_for(self, interval: CandleInterval) -> CandleSaveResult:
        return dict(self.saved).get(interval, CandleSaveResult(inserted=0, identical=0))


class HistoricalIngestionAborted(Exception):  # noqa: N818 - "aborted" is the outcome being named
    """The run stopped on a failure that retrying cannot fix.

    Carries the partial report, so what was stored before the failure is still
    known. The broker error is the ``__cause__``.
    """

    def __init__(self, report: HistoricalIngestionReport) -> None:
        failure = report.failure
        where = (
            f" in window {failure.window.start.isoformat()}-{failure.window.end.isoformat()}"
            if failure
            else ""
        )
        super().__init__(f"historical ingestion aborted{where}: {failure.error if failure else ''}")
        self.report = report


def _merge(total: CandleSaveResult, more: CandleSaveResult) -> CandleSaveResult:
    return CandleSaveResult(
        inserted=total.inserted + more.inserted,
        identical=total.identical + more.identical,
        conflicts=total.conflicts + more.conflicts,
    )


def _ist_dates(window: RequestWindow) -> set[date]:
    first = to_ist(window.start).date()
    last = to_ist(window.end - timedelta(microseconds=1)).date()
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


class HistoricalIngestionService:
    """Ingests one instrument's history. One instance may run many ranges."""

    def __init__(
        self,
        *,
        source: HistoricalCandleSource,
        repository: MarketDataRepository,
        throttle: RequestThrottle,
        retry_policy: RetryPolicy,
        sleep: Sleep,
        calendar: MarketSessionCalendar | None = None,
        max_window: timedelta = DEFAULT_MINUTE_WINDOW,
        aggregate_intervals: Sequence[CandleInterval] = _AGGREGATES,
    ) -> None:
        unsupported = [i for i in aggregate_intervals if i not in _AGGREGATES]
        if unsupported:
            raise ValueError(f"cannot aggregate minutes into {unsupported}")
        self._source = source
        self._repository = repository
        self._throttle = throttle
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._calendar = calendar or MarketSessionCalendar.nse_equity()
        self._max_window = max_window
        self._aggregates = tuple(aggregate_intervals)

    async def ingest(
        self, instrument: Instrument, *, start: datetime, end: datetime, as_of: datetime
    ) -> HistoricalIngestionReport:
        """Fetch, aggregate and store ``[start, end)``. See the module docstring."""
        start, end, as_of = ensure_utc(start), ensure_utc(end), ensure_utc(as_of)
        for name, moment in (("start", start), ("end", end)):
            if moment != bucket_start(moment, CandleInterval.M15):
                raise ValueError(
                    f"{name} ({moment.isoformat()}) must fall on a 15-minute boundary, so no "
                    "5m or 15m slot is split between two request windows"
                )
        windows = plan_windows(start, end, max_span=self._max_window)

        intervals = (CandleInterval.M1, *self._aggregates)
        saved = {interval: CandleSaveResult(inserted=0, identical=0) for interval in intervals}
        incomplete: list[tuple[CandleInterval, IncompleteSlot]] = []
        attempted: list[WindowReport] = []
        covered: set[date] = set()
        with_data: set[date] = set()

        def report() -> HistoricalIngestionReport:
            return HistoricalIngestionReport(
                instrument=instrument,
                start=start,
                end=end,
                as_of=as_of,
                windows_planned=len(windows),
                windows=tuple(attempted),
                saved=tuple((interval, saved[interval]) for interval in intervals),
                incomplete_slots=tuple(incomplete),
                no_data_sessions=tuple(
                    sorted(day for day in covered - with_data if self._calendar.is_trading_day(day))
                ),
            )

        for window in windows:
            try:
                minutes = await self._fetch(instrument, window, as_of)
            except RETRYABLE_ERRORS as exc:
                attempted.append(self._failed(window, exc))
                logger.warning("historical_ingestion_stopped", extra=self._log(instrument, window))
                return report()
            except (ZerodhaError, ValueError) as exc:
                attempted.append(self._failed(window, exc))
                raise HistoricalIngestionAborted(report()) from exc

            saved[CandleInterval.M1] = _merge(
                saved[CandleInterval.M1], self._repository.save_historical_candles(minutes)
            )
            for interval in self._aggregates:
                aggregated = aggregate_minutes(minutes, interval)
                saved[interval] = _merge(
                    saved[interval],
                    self._repository.save_historical_candles(aggregated.candles),
                )
                incomplete.extend((interval, slot) for slot in aggregated.incomplete)

            covered |= _ist_dates(window)
            with_data |= {to_ist(m.start_at).date() for m in minutes}
            outcome = WindowOutcome.INGESTED if minutes else WindowOutcome.NO_DATA
            attempted.append(WindowReport(window, outcome, bars_fetched=len(minutes)))
            logger.info(
                "historical_window_ingested",
                extra={**self._log(instrument, window), "bars": len(minutes)},
            )

        return report()

    async def _fetch(
        self, instrument: Instrument, window: RequestWindow, as_of: datetime
    ) -> list[Candle]:
        async def attempt() -> list[Candle]:
            await self._throttle.acquire()
            return await self._source.fetch_historical_candles(
                instrument, CandleInterval.M1, start=window.start, end=window.end, as_of=as_of
            )

        return await with_retries(attempt, policy=self._retry_policy, sleep=self._sleep)

    @staticmethod
    def _failed(window: RequestWindow, exc: Exception) -> WindowReport:
        return WindowReport(window, WindowOutcome.FAILED, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _log(instrument: Instrument, window: RequestWindow) -> dict[str, object]:
        return {
            "instrument_token": instrument.instrument_token,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
        }

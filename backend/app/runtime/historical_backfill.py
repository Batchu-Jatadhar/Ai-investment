"""Historical backfill command - read-only toward Zerodha.

    aitrade-backfill --symbol NSE:RELIANCE --from 2026-01-01 --to 2026-06-30

Fetches 1-minute history for one instrument, derives 5m and 15m bars from it,
stores all three conflict-safely and prints what happened. The work is done by
:class:`~app.services.historical_ingestion.HistoricalIngestionService`; this
module only parses arguments, wires the pieces and reports.

It places no orders and cannot reach code that could. The only broker requests
on this path are ``GET /instruments/historical/...``. The instrument is resolved
from the instrument master already stored by ``aitrade-marketdata``.

.. rubric:: Ranges

``--from`` and ``--to`` take either a date or a timestamp with an explicit UTC
offset. A date means whole IST days, and ``--to`` is then inclusive: ``--from
2026-01-01 --to 2026-06-30`` covers both days. A timestamp is used exactly as
given, ``--to`` exclusive, and must fall on a 15-minute boundary. Naive
timestamps are refused - guessing their zone is how a range ends up shifted by
five and a half hours.

``--as-of`` is the cutoff after which bars count as unfinished. When omitted it
is read from the system clock once, here at the command boundary, and passed
down explicitly; nothing below this module reads a clock.

.. rubric:: Exit codes

``0`` every window stored; ``1`` stopped after a transient failure exhausted
its retries; ``2`` bad arguments, configuration or unknown instrument; ``3``
aborted on an authentication, input or protocol failure.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import TextIO

from app.adapters.zerodha.pacing import RequestThrottle, RetryPolicy, Sleep
from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging
from app.core.time import IST, Clock, SystemClock, ensure_utc, to_ist
from app.domain.market.ports import HistoricalCandleSource, MarketDataRepository
from app.infrastructure.db import get_session_factory
from app.infrastructure.repositories.market_data import SqlMarketDataRepository
from app.runtime.market_data import build_rest_client
from app.services.historical_ingestion import (
    HistoricalIngestionAborted,
    HistoricalIngestionReport,
    HistoricalIngestionService,
    WindowOutcome,
)

__all__ = ["format_report", "main", "run"]

EXIT_COMPLETED, EXIT_INCOMPLETE, EXIT_USAGE, EXIT_ABORTED = 0, 1, 2, 3

#: How many incomplete slots are listed individually before summarising.
_SLOT_LINES = 10


class _UsageError(ValueError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aitrade-backfill",
        description=(
            "Read-only historical candle backfill for one instrument. Fetches 1m history "
            "from Zerodha, derives 5m/15m locally, stores conflict-safely. Places no orders."
        ),
    )
    parser.add_argument("--symbol", required=True, help="EXCHANGE:TRADINGSYMBOL, e.g. NSE:RELIANCE")
    parser.add_argument(
        "--from",
        dest="start",
        required=True,
        help="YYYY-MM-DD (IST day) or ISO timestamp with offset",
    )
    parser.add_argument(
        "--to",
        dest="end",
        required=True,
        help="YYYY-MM-DD (IST day, inclusive) or ISO timestamp with offset (exclusive)",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        help="ISO timestamp with offset; bars ending after it are unfinished (default: now)",
    )
    return parser


def _moment(raw: str, flag: str, *, inclusive_date: bool = False) -> datetime:
    """A date as an IST midnight, or a timestamp that must carry its own offset."""
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        pass
    else:
        if inclusive_date:
            day += timedelta(days=1)
        return ensure_utc(datetime.combine(day, time(0), IST))
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _UsageError(f"{flag} {raw!r} is not a date or ISO timestamp") from exc
    if moment.tzinfo is None:
        raise _UsageError(f"{flag} {raw!r} has no UTC offset; give one, e.g. +05:30")
    return ensure_utc(moment)


def _symbol(raw: str) -> tuple[str, str]:
    exchange, _, tradingsymbol = raw.partition(":")
    if not exchange or not tradingsymbol:
        raise _UsageError(f"--symbol {raw!r} must be EXCHANGE:TRADINGSYMBOL, e.g. NSE:RELIANCE")
    return exchange.upper(), tradingsymbol.upper()


def _ist(moment: datetime) -> str:
    return to_ist(moment).strftime("%Y-%m-%d %H:%M IST")


def format_report(report: HistoricalIngestionReport) -> str:
    """A human-readable summary of an ingestion run. No candles are listed."""
    instrument = report.instrument
    outcomes = [w.outcome for w in report.windows]
    lines = [
        f"Historical backfill {instrument.exchange}:{instrument.tradingsymbol} "
        f"(token {instrument.instrument_token})",
        f"  range      {_ist(report.start)} -> {_ist(report.end)}  (as of {_ist(report.as_of)})",
        f"  windows    planned {report.windows_planned}, "
        f"ingested {outcomes.count(WindowOutcome.INGESTED)}, "
        f"no-data {outcomes.count(WindowOutcome.NO_DATA)}, "
        f"failed {report.windows_failed}, bars fetched {report.bars_fetched}",
    ]
    for interval, saved in report.saved:
        lines.append(
            f"  {interval.value:<4}       inserted {saved.inserted}, identical {saved.identical}, "
            f"conflicts {saved.conflict_count}"
        )
    lines.append(f"  incomplete slots {len(report.incomplete_slots)}")
    for interval, slot in report.incomplete_slots[:_SLOT_LINES]:
        missing = ", ".join(to_ist(m).strftime("%H:%M") for m in slot.missing)
        lines.append(f"    {interval.value} {_ist(slot.start_at)} missing {missing}")
    if len(report.incomplete_slots) > _SLOT_LINES:
        lines.append(f"    ... and {len(report.incomplete_slots) - _SLOT_LINES} more")
    sessions = ", ".join(day.isoformat() for day in report.no_data_sessions) or "none"
    lines.append(f"  no-data sessions {sessions}")

    failure = report.failure
    if report.completed:
        lines.append("  status     COMPLETED")
    elif failure is not None:
        lines.append(
            f"  status     FAILED in window {_ist(failure.window.start)} -> "
            f"{_ist(failure.window.end)}: {failure.error}"
        )
        lines.append(
            f"             {report.windows_planned - len(report.windows)} later window(s) "
            "not attempted; earlier windows are stored and re-running is safe"
        )
    return "\n".join(lines)


async def run(
    argv: Sequence[str],
    *,
    settings: Settings | None = None,
    source: HistoricalCandleSource | None = None,
    repository: MarketDataRepository | None = None,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the command. Collaborators may be injected; defaults are the real ones."""
    out = out or sys.stdout
    args = _parser().parse_args(list(argv))
    clock = clock or SystemClock()

    try:
        exchange, tradingsymbol = _symbol(args.symbol)
        start = _moment(args.start, "--from")
        end = _moment(args.end, "--to", inclusive_date=True)
        as_of = _moment(args.as_of, "--as-of") if args.as_of else clock.now()
        if start >= end:
            raise _UsageError(f"--from ({_ist(start)}) must be before --to ({_ist(end)})")
    except _UsageError as exc:
        print(f"error: {exc}", file=out)
        return EXIT_USAGE

    close_source = None
    if source is None:
        settings = settings or get_settings()
        configure_logging(level=settings.log_level, fmt=settings.log_format.value)
        if not settings.zerodha_configured:
            print("error: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required", file=out)
            return EXIT_USAGE
        client = build_rest_client(settings)
        source, close_source = client, client
    repository = repository or SqlMarketDataRepository(get_session_factory())

    instrument = repository.get_instrument_by_symbol(exchange, tradingsymbol)
    if instrument is None:
        print(
            f"error: {exchange}:{tradingsymbol} is not in the stored instrument master; "
            "refresh it with aitrade-marketdata first",
            file=out,
        )
        return EXIT_USAGE

    sleep = sleep or asyncio.sleep
    service = HistoricalIngestionService(
        source=source,
        repository=repository,
        throttle=RequestThrottle(clock, sleep),
        retry_policy=RetryPolicy(),
        sleep=sleep,
    )
    try:
        report = await service.ingest(instrument, start=start, end=end, as_of=as_of)
    except HistoricalIngestionAborted as exc:
        print(format_report(exc.report), file=out)
        print(f"  aborted    {type(exc.__cause__).__name__}: {exc.__cause__}", file=out)
        return EXIT_ABORTED
    except ValueError as exc:
        print(f"error: {exc}", file=out)
        return EXIT_USAGE
    finally:
        if close_source is not None:
            await close_source.aclose()

    print(format_report(report), file=out)
    return EXIT_COMPLETED if report.completed else EXIT_INCOMPLETE


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

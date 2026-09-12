"""Kite historical candles: request encoding and response parsing.

Vendor-specific, so it lives in the adapter. The domain receives ordinary
:class:`~app.domain.market.models.Candle` objects and never sees Kite's
interval names, its IST wall-clock query format or its row arrays.

Reference: https://kite.trade/docs/connect/v3/historical/

The response is treated as untrusted input. Every row is checked before it
becomes a candle, and a single bad row fails the whole response with
:class:`ZerodhaProtocolError` rather than being skipped: a gap silently left in
the middle of a price series is worse than a fetch that visibly failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.adapters.zerodha.errors import ZerodhaProtocolError
from app.core.time import ensure_utc, to_ist
from app.domain.market.models import Candle, CandleInterval, CandleStatus, Instrument

__all__ = [
    "HISTORICAL_SOURCE",
    "KITE_INTERVALS",
    "historical_path",
    "ist_query_time",
    "parse_historical_candles",
]

#: Provenance label on every candle built here, distinct from the live stream.
HISTORICAL_SOURCE = "zerodha_historical"

#: The Kite interval name for each interval the domain supports.
KITE_INTERVALS: dict[CandleInterval, str] = {
    CandleInterval.M1: "minute",
    CandleInterval.M5: "5minute",
    CandleInterval.M15: "15minute",
}

#: Kite's documented row: timestamp, open, high, low, close, volume. ``oi`` is
#: never requested, so a seventh column is a malformed row.
_ROW_LENGTH = 6
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def historical_path(instrument_token: int, interval: CandleInterval) -> str:
    """The endpoint path. Raises ``ValueError`` for an interval Kite is not asked for."""
    kite_interval = KITE_INTERVALS.get(interval)
    if kite_interval is None:
        raise ValueError(
            f"interval {interval!r} is not supported for historical candles; "
            f"supported: {sorted(i.value for i in KITE_INTERVALS)}"
        )
    return f"/instruments/historical/{instrument_token}/{kite_interval}"


def ist_query_time(moment: datetime) -> str:
    """``yyyy-mm-dd hh:mm:ss`` in IST, the form Kite's ``from``/``to`` expect.

    Kite returns +05:30 timestamps and reads its query bounds as exchange-local
    wall time. Taking an aware datetime and converting here means a caller can
    never pass a naive value that is silently read in the wrong zone.
    """
    return to_ist(ensure_utc(moment)).strftime("%Y-%m-%d %H:%M:%S")


def _fail(index: int, detail: str, row: object) -> ZerodhaProtocolError:
    return ZerodhaProtocolError(f"historical candle row {index} {detail}", row=repr(row)[:200])


def _price(value: object, index: int, name: str, row: object) -> Decimal:
    # bool is an int subclass and float would already have lost precision; the
    # client parses JSON floats as Decimal, so neither may reach a price.
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise _fail(index, f"has a non-numeric {name}", row)
    price = Decimal(value)
    if not price.is_finite() or price <= 0:
        raise _fail(index, f"has a non-positive {name} ({value})", row)
    return price


def parse_historical_candles(
    data: Any,
    *,
    instrument: Instrument,
    interval: CandleInterval,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> list[Candle]:
    """Turn Kite's ``data`` object into completed candles inside ``[start, end)``.

    Every row is validated first, in response order. Only then are rows outside
    the requested window dropped - Kite's handling of the ``to`` bound is not
    documented, so the window is enforced here - along with any bar that had not
    finished by ``as_of``, since a fetch that reaches the current session
    returns its still-forming last bar.
    """
    start, end, as_of = ensure_utc(start), ensure_utc(end), ensure_utc(as_of)

    rows = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ZerodhaProtocolError("historical response had no 'candles' list")

    candles: list[Candle] = []
    previous: datetime | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, str) or len(row) != _ROW_LENGTH:
            raise _fail(index, f"must have {_ROW_LENGTH} fields", row)
        stamp, *ohlc, volume = row

        if not isinstance(stamp, str):
            raise _fail(index, "has a non-string timestamp", row)
        try:
            started = ensure_utc(datetime.strptime(stamp, _TIMESTAMP_FORMAT))
        except ValueError as exc:
            raise _fail(index, f"has an unparseable timestamp {stamp!r}", row) from exc

        if int(started.timestamp()) % interval.seconds or started.microsecond:
            raise _fail(index, f"starts at {stamp}, off the {interval.value} grid", row)
        if previous is not None and started == previous:
            raise _fail(index, f"duplicates the bar at {stamp}", row)
        if previous is not None and started < previous:
            raise _fail(index, f"at {stamp} is out of order", row)
        previous = started

        open_, high, low, close = (
            _price(value, index, name, row)
            for value, name in zip(ohlc, ("open", "high", "low", "close"), strict=True)
        )
        if high < low:
            raise _fail(index, f"has high {high} below low {low}", row)
        if not (low <= open_ <= high and low <= close <= high):
            raise _fail(index, "has an open or close outside its high-low range", row)
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise _fail(index, "has an invalid volume", row)

        candle = Candle(
            instrument_token=instrument.instrument_token,
            interval=interval,
            start_at=started,
            end_at=started + interval.delta,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            status=CandleStatus.COMPLETED,
            source=HISTORICAL_SOURCE,
            tradingsymbol=instrument.tradingsymbol,
            exchange=instrument.exchange,
        )
        if start <= candle.start_at < end and candle.end_at <= as_of:
            candles.append(candle)

    return candles

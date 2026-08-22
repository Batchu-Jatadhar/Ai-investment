"""Zerodha Kite streaming binary protocol.

Implemented from the official specification at
https://kite.trade/docs/connect/v3/websocket/ .

Frame layout (big-endian throughout)::

    [0:2]   int16   number of packets in this frame
    then, for each packet:
    [n:n+2] int16   packet length
    [.....] bytes   packet payload

Packet length selects the layout - there is no explicit type field::

      8 bytes   LTP mode
     28 bytes   index, quote mode
     32 bytes   index, full mode
     44 bytes   quote mode
    184 bytes   full mode (quote + timestamps + OI + 5x5 market depth)

Price scaling depends on the segment, which is encoded in the low byte of the
instrument token::

    CDS (3)  -> divide by 10,000,000     (four decimal currency quotes)
    BCD (6)  -> divide by 10,000
    others   -> divide by 100

Nothing here raises on bad input. Malformed frames are reported as structured
findings so the data-quality layer can count them; the stream must not die
because one packet was short.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.market.models import (
    DepthLevel,
    MarketDepth,
    MarketTick,
    TickMode,
)

__all__ = [
    "PACKET_FULL",
    "PACKET_INDEX_FULL",
    "PACKET_INDEX_QUOTE",
    "PACKET_LTP",
    "PACKET_QUOTE",
    "ParsedFrame",
    "ProtocolFinding",
    "divisor_for_token",
    "is_heartbeat",
    "is_index_token",
    "parse_frame",
    "segment_of_token",
]

# Segment codes carried in the low byte of an instrument token.
SEGMENT_NSE = 1
SEGMENT_NFO = 2
SEGMENT_CDS = 3
SEGMENT_BSE = 4
SEGMENT_BFO = 5
SEGMENT_BCD = 6
SEGMENT_MCX = 7
SEGMENT_MCXSX = 8
SEGMENT_INDICES = 9

PACKET_LTP = 8
PACKET_INDEX_QUOTE = 28
PACKET_INDEX_FULL = 32
PACKET_QUOTE = 44
PACKET_FULL = 184

_DEPTH_ENTRY = 12
_DEPTH_LEVELS = 5

_DIVISOR_DEFAULT = Decimal(100)
_DIVISOR_CDS = Decimal(10_000_000)
_DIVISOR_BCD = Decimal(10_000)


def segment_of_token(instrument_token: int) -> int:
    """Segment code encoded in the low byte of the token."""
    return instrument_token & 0xFF


def is_index_token(instrument_token: int) -> bool:
    """Indices use a different packet layout and are not directly tradable."""
    return segment_of_token(instrument_token) == SEGMENT_INDICES


def divisor_for_token(instrument_token: int) -> Decimal:
    """Price scaling factor for the token's segment."""
    segment = segment_of_token(instrument_token)
    if segment == SEGMENT_CDS:
        return _DIVISOR_CDS
    if segment == SEGMENT_BCD:
        return _DIVISOR_BCD
    return _DIVISOR_DEFAULT


def is_heartbeat(payload: bytes) -> bool:
    """The server sends short keep-alive frames that carry no packets."""
    return len(payload) < 2


@dataclass(frozen=True, slots=True)
class ProtocolFinding:
    """A malformed or unusable piece of a frame."""

    reason: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    ticks: tuple[MarketTick, ...] = ()
    findings: tuple[ProtocolFinding, ...] = ()
    heartbeat: bool = False

    @property
    def is_clean(self) -> bool:
        return not self.findings


def _price(raw: int, divisor: Decimal) -> Decimal:
    return Decimal(raw) / divisor


def _timestamp(raw: int) -> datetime | None:
    """Epoch seconds from the exchange. Zero means 'not supplied'."""
    if raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_depth(chunk: bytes, divisor: Decimal) -> MarketDepth:
    bids: list[DepthLevel] = []
    asks: list[DepthLevel] = []
    for index in range(_DEPTH_LEVELS * 2):
        offset = index * _DEPTH_ENTRY
        quantity, raw_price, orders = struct.unpack_from(">iiH", chunk, offset)
        level = DepthLevel(price=_price(raw_price, divisor), quantity=quantity, orders=orders)
        (bids if index < _DEPTH_LEVELS else asks).append(level)
    return MarketDepth(bids=tuple(bids), asks=tuple(asks))


def _parse_packet(
    packet: bytes, received_at: datetime, source: str
) -> MarketTick | ProtocolFinding:
    size = len(packet)
    if size < PACKET_LTP:
        return ProtocolFinding("packet_too_short", {"length": size})

    token = struct.unpack_from(">i", packet, 0)[0]
    divisor = divisor_for_token(token)
    index_packet = size in (PACKET_INDEX_QUOTE, PACKET_INDEX_FULL)

    if size == PACKET_LTP:
        (last_price,) = struct.unpack_from(">i", packet, 4)
        return MarketTick(
            instrument_token=token,
            last_price=_price(last_price, divisor),
            mode=TickMode.LTP,
            received_at=received_at,
            is_index=is_index_token(token),
            source=source,
        )

    if index_packet:
        # token, ltp, high, low, open, close, price_change [, exchange_ts]
        values = struct.unpack_from(">6i", packet, 0)
        exchange_ts = None
        if size == PACKET_INDEX_FULL:
            exchange_ts = _timestamp(struct.unpack_from(">i", packet, 28)[0])
        return MarketTick(
            instrument_token=token,
            last_price=_price(values[1], divisor),
            mode=TickMode.FULL if size == PACKET_INDEX_FULL else TickMode.QUOTE,
            received_at=received_at,
            exchange_timestamp=exchange_ts,
            high_price=_price(values[2], divisor),
            low_price=_price(values[3], divisor),
            open_price=_price(values[4], divisor),
            close_price=_price(values[5], divisor),
            is_index=True,
            source=source,
        )

    if size not in (PACKET_QUOTE, PACKET_FULL):
        return ProtocolFinding("unknown_packet_size", {"length": size, "token": token})

    quote = struct.unpack_from(">11i", packet, 0)
    tick_kwargs: dict[str, object] = {
        "instrument_token": token,
        "last_price": _price(quote[1], divisor),
        "last_quantity": quote[2],
        "average_price": _price(quote[3], divisor),
        "volume": quote[4],
        "total_buy_quantity": quote[5],
        "total_sell_quantity": quote[6],
        "open_price": _price(quote[7], divisor),
        "high_price": _price(quote[8], divisor),
        "low_price": _price(quote[9], divisor),
        "close_price": _price(quote[10], divisor),
        "received_at": received_at,
        "is_index": False,
        "source": source,
    }

    if size == PACKET_QUOTE:
        return MarketTick(mode=TickMode.QUOTE, **tick_kwargs)  # type: ignore[arg-type]

    extra = struct.unpack_from(">5i", packet, 44)
    tick_kwargs.update(
        {
            "mode": TickMode.FULL,
            "last_traded_at": _timestamp(extra[0]),
            "open_interest": extra[1],
            "exchange_timestamp": _timestamp(extra[4]),
            "depth": _parse_depth(packet[64:PACKET_FULL], divisor),
        }
    )
    return MarketTick(**tick_kwargs)  # type: ignore[arg-type]


def parse_frame(payload: bytes, received_at: datetime, *, source: str = "zerodha") -> ParsedFrame:
    """Decode one binary WebSocket frame into ticks.

    Returns findings instead of raising: a short or unknown packet must be
    counted and moved past, not allowed to kill the stream.
    """
    if is_heartbeat(payload):
        return ParsedFrame(heartbeat=True)

    findings: list[ProtocolFinding] = []
    ticks: list[MarketTick] = []

    (count,) = struct.unpack_from(">h", payload, 0)
    if count <= 0:
        return ParsedFrame(heartbeat=True)

    offset = 2
    total = len(payload)
    for index in range(count):
        if offset + 2 > total:
            findings.append(
                ProtocolFinding(
                    "truncated_length_header",
                    {"packet_index": index, "offset": offset, "frame_length": total},
                )
            )
            break
        (length,) = struct.unpack_from(">h", payload, offset)
        offset += 2
        if length <= 0 or offset + length > total:
            findings.append(
                ProtocolFinding(
                    "truncated_packet",
                    {"packet_index": index, "declared_length": length, "remaining": total - offset},
                )
            )
            break
        packet = payload[offset : offset + length]
        offset += length

        try:
            result = _parse_packet(packet, received_at, source)
        except struct.error as exc:  # defensive: a length that passes but misaligns
            findings.append(
                ProtocolFinding("unpack_error", {"packet_index": index, "error": str(exc)})
            )
            continue

        if isinstance(result, ProtocolFinding):
            findings.append(result)
        else:
            ticks.append(result)

    return ParsedFrame(ticks=tuple(ticks), findings=tuple(findings))

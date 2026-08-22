"""Deterministic fixtures for the market-data tests.

Nothing here touches the live Zerodha API. Binary frames are constructed byte
for byte from the documented layout, so the parser is tested against the spec
rather than against a recording of itself.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.market.models import Instrument, MarketTick, TickMode
from app.infrastructure.repositories.market_data import SqlMarketDataRepository

# Real-shaped tokens. The low byte is the segment code:
#   & 0xFF == 1 -> NSE equity, == 9 -> INDICES
RELIANCE_TOKEN = 738561  # 738561 & 0xFF == 1
INFY_TOKEN = 408065  # 408065 & 0xFF == 1
NIFTY50_TOKEN = 256265  # 256265 & 0xFF == 9  (index)
BANKNIFTY_TOKEN = 260105  # 260105 & 0xFF == 9  (index)
UNKNOWN_TOKEN = 999999

BASE_TIME = datetime(2026, 8, 21, 4, 0, 0, tzinfo=UTC)  # 09:30 IST, mid-session


def make_instrument(
    token: int,
    tradingsymbol: str,
    *,
    exchange: str = "NSE",
    segment: str = "NSE",
    instrument_type: str = "EQ",
    lot_size: int = 1,
    tick_size: str = "0.05",
    retrieved_at: datetime | None = None,
) -> Instrument:
    return Instrument(
        instrument_token=token,
        exchange_token=token >> 8,
        tradingsymbol=tradingsymbol,
        name=tradingsymbol.title(),
        exchange=exchange,
        segment=segment,
        instrument_type=instrument_type,
        tick_size=Decimal(tick_size),
        lot_size=lot_size,
        source="test",
        retrieved_at=retrieved_at or BASE_TIME,
    )


@pytest.fixture
def instruments() -> list[Instrument]:
    return [
        make_instrument(RELIANCE_TOKEN, "RELIANCE"),
        make_instrument(INFY_TOKEN, "INFY"),
        make_instrument(
            NIFTY50_TOKEN, "NIFTY 50", segment="INDICES", instrument_type="EQ", lot_size=0
        ),
        make_instrument(
            BANKNIFTY_TOKEN,
            "NIFTY BANK",
            segment="INDICES",
            instrument_type="EQ",
            lot_size=0,
        ),
    ]


@pytest.fixture
def resolver(instruments: list[Instrument]):
    by_token = {i.instrument_token: i for i in instruments}
    return by_token.get


#: Market-data tables, in an order safe to delete from.
_MARKET_TABLES = (
    "market_tick",
    "candle",
    "data_quality_event",
    "data_gap",
    "connection_event",
    "instrument",
)


@pytest.fixture
def repository(wired_settings) -> SqlMarketDataRepository:  # noqa: ANN001
    """A repository over an empty market-data schema.

    The migrated database is session-scoped, so each test truncates the Phase 1
    tables first. Without this, rows leak between tests and assertions about
    counts and "latest" rows quietly become order-dependent.
    """
    from sqlalchemy import text

    from app.infrastructure.db import get_engine, get_session_factory

    engine = get_engine(wired_settings)
    with engine.begin() as conn:
        for table in _MARKET_TABLES:
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table list
    return SqlMarketDataRepository(get_session_factory())


# --------------------------------------------------------------------------- #
# Binary frame construction, from the documented layout
# --------------------------------------------------------------------------- #


def ltp_packet(token: int, last_price_paise: int) -> bytes:
    """8-byte LTP packet."""
    return struct.pack(">ii", token, last_price_paise)


def quote_packet(
    token: int,
    last_price_paise: int,
    *,
    last_quantity: int = 10,
    average_price: int = 0,
    volume: int = 1000,
    total_buy: int = 500,
    total_sell: int = 400,
    open_: int = 0,
    high: int = 0,
    low: int = 0,
    close: int = 0,
) -> bytes:
    """44-byte quote packet."""
    return struct.pack(
        ">11i",
        token,
        last_price_paise,
        last_quantity,
        average_price or last_price_paise,
        volume,
        total_buy,
        total_sell,
        open_ or last_price_paise,
        high or last_price_paise,
        low or last_price_paise,
        close or last_price_paise,
    )


def full_packet(
    token: int,
    last_price_paise: int,
    *,
    volume: int = 1000,
    exchange_timestamp: datetime | None = None,
    last_traded_at: datetime | None = None,
    open_interest: int = 0,
    depth: bool = True,
    **quote_kwargs: int,
) -> bytes:
    """184-byte full packet: quote + timestamps + OI + 5x5 depth."""
    head = quote_packet(token, last_price_paise, volume=volume, **quote_kwargs)
    ltt = int(last_traded_at.timestamp()) if last_traded_at else 0
    ets = int(exchange_timestamp.timestamp()) if exchange_timestamp else 0
    middle = struct.pack(">5i", ltt, open_interest, 0, 0, ets)

    body = b""
    for level in range(10):
        if depth:
            offset = level % 5
            price = (
                last_price_paise - (offset + 1) if level < 5 else last_price_paise + (offset + 1)
            )
            body += struct.pack(">iiHH", 100 * (offset + 1), price, offset + 1, 0)
        else:
            body += struct.pack(">iiHH", 0, 0, 0, 0)
    assert len(head) + len(middle) + len(body) == 184
    return head + middle + body


def index_packet(
    token: int,
    last_price_paise: int,
    *,
    exchange_timestamp: datetime | None = None,
    high: int = 0,
    low: int = 0,
    open_: int = 0,
    close: int = 0,
    price_change: int = 0,
) -> bytes:
    """28-byte index quote, or 32-byte index full when a timestamp is supplied."""
    packet = struct.pack(
        ">6i",
        token,
        last_price_paise,
        high or last_price_paise,
        low or last_price_paise,
        open_ or last_price_paise,
        close or last_price_paise,
    )
    packet += struct.pack(">i", price_change)
    if exchange_timestamp is not None:
        packet += struct.pack(">i", int(exchange_timestamp.timestamp()))
    return packet


def frame(*packets: bytes) -> bytes:
    """Wrap packets into a WebSocket binary frame."""
    out = struct.pack(">h", len(packets))
    for packet in packets:
        out += struct.pack(">h", len(packet)) + packet
    return out


HEARTBEAT = b"\x00"


def make_tick(
    token: int = RELIANCE_TOKEN,
    price: str = "1400.00",
    *,
    at: datetime | None = None,
    volume: int | None = 1000,
    mode: TickMode = TickMode.FULL,
    symbol: str | None = "RELIANCE",
) -> MarketTick:
    moment = at or BASE_TIME
    return MarketTick(
        instrument_token=token,
        last_price=Decimal(price),
        mode=mode,
        received_at=moment,
        exchange_timestamp=moment,
        tradingsymbol=symbol,
        exchange="NSE",
        volume=volume,
        last_quantity=5,
        source="test",
    )

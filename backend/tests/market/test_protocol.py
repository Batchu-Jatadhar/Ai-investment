"""Zerodha binary tick protocol: normalization and malformed input."""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.adapters.zerodha.protocol import (
    divisor_for_token,
    is_heartbeat,
    is_index_token,
    parse_frame,
    segment_of_token,
)
from app.domain.market.models import TickMode
from tests.market.conftest import (
    BASE_TIME,
    HEARTBEAT,
    INFY_TOKEN,
    NIFTY50_TOKEN,
    RELIANCE_TOKEN,
    frame,
    full_packet,
    index_packet,
    ltp_packet,
    quote_packet,
)


class TestTokenEncoding:
    def test_segment_is_the_low_byte(self) -> None:
        assert segment_of_token(RELIANCE_TOKEN) == 1  # NSE
        assert segment_of_token(NIFTY50_TOKEN) == 9  # INDICES

    def test_index_detection(self) -> None:
        assert is_index_token(NIFTY50_TOKEN) is True
        assert is_index_token(RELIANCE_TOKEN) is False

    def test_price_divisor_by_segment(self) -> None:
        assert divisor_for_token(RELIANCE_TOKEN) == Decimal(100)
        # Currency segments quote four decimals.
        cds_token = (1234 << 8) | 3
        bcd_token = (1234 << 8) | 6
        assert divisor_for_token(cds_token) == Decimal(10_000_000)
        assert divisor_for_token(bcd_token) == Decimal(10_000)


class TestHeartbeat:
    def test_single_byte_frame_is_a_heartbeat(self) -> None:
        assert is_heartbeat(HEARTBEAT) is True
        parsed = parse_frame(HEARTBEAT, BASE_TIME)
        assert parsed.heartbeat is True
        assert parsed.ticks == ()

    def test_zero_packet_frame_is_treated_as_a_heartbeat(self) -> None:
        parsed = parse_frame(struct.pack(">h", 0), BASE_TIME)
        assert parsed.heartbeat is True


class TestLtpMode:
    def test_decodes_price_and_token(self) -> None:
        parsed = parse_frame(frame(ltp_packet(RELIANCE_TOKEN, 140055)), BASE_TIME)
        assert len(parsed.ticks) == 1
        tick = parsed.ticks[0]
        assert tick.instrument_token == RELIANCE_TOKEN
        assert tick.last_price == Decimal("1400.55")
        assert tick.mode is TickMode.LTP
        assert tick.exchange_timestamp is None
        assert tick.volume is None

    def test_ltp_falls_back_to_receive_time(self) -> None:
        tick = parse_frame(frame(ltp_packet(INFY_TOKEN, 100000)), BASE_TIME).ticks[0]
        assert tick.event_time == BASE_TIME
        assert tick.has_exchange_time is False


class TestQuoteMode:
    def test_decodes_all_quote_fields(self) -> None:
        packet = quote_packet(
            RELIANCE_TOKEN,
            140000,
            last_quantity=25,
            average_price=139950,
            volume=987654,
            total_buy=1111,
            total_sell=2222,
            open_=139000,
            high=141000,
            low=138500,
            close=139500,
        )
        tick = parse_frame(frame(packet), BASE_TIME).ticks[0]
        assert tick.mode is TickMode.QUOTE
        assert tick.last_price == Decimal("1400.00")
        assert tick.last_quantity == 25
        assert tick.average_price == Decimal("1399.50")
        assert tick.volume == 987654
        assert tick.total_buy_quantity == 1111
        assert tick.total_sell_quantity == 2222
        assert tick.open_price == Decimal("1390.00")
        assert tick.high_price == Decimal("1410.00")
        assert tick.low_price == Decimal("1385.00")
        assert tick.close_price == Decimal("1395.00")
        assert tick.depth is None


class TestFullMode:
    def test_decodes_timestamps_oi_and_depth(self) -> None:
        exchange_ts = datetime(2026, 8, 21, 4, 0, 5, tzinfo=UTC)
        traded_at = exchange_ts - timedelta(seconds=1)
        packet = full_packet(
            RELIANCE_TOKEN,
            140000,
            volume=555,
            exchange_timestamp=exchange_ts,
            last_traded_at=traded_at,
            open_interest=4242,
        )
        assert len(packet) == 184
        tick = parse_frame(frame(packet), BASE_TIME).ticks[0]
        assert tick.mode is TickMode.FULL
        assert tick.exchange_timestamp == exchange_ts
        assert tick.last_traded_at == traded_at
        assert tick.open_interest == 4242
        assert tick.depth is not None
        assert len(tick.depth.bids) == 5
        assert len(tick.depth.asks) == 5

    def test_event_time_prefers_the_exchange_timestamp(self) -> None:
        exchange_ts = datetime(2026, 8, 21, 3, 59, 0, tzinfo=UTC)
        packet = full_packet(RELIANCE_TOKEN, 140000, exchange_timestamp=exchange_ts)
        tick = parse_frame(frame(packet), BASE_TIME).ticks[0]
        assert tick.event_time == exchange_ts
        assert tick.event_time != tick.received_at

    def test_zero_timestamp_is_absent_not_epoch(self) -> None:
        tick = parse_frame(
            frame(full_packet(RELIANCE_TOKEN, 140000, exchange_timestamp=None)), BASE_TIME
        ).ticks[0]
        assert tick.exchange_timestamp is None
        assert tick.event_time == BASE_TIME

    def test_depth_is_ordered_bids_then_asks(self) -> None:
        tick = parse_frame(frame(full_packet(RELIANCE_TOKEN, 140000)), BASE_TIME).ticks[0]
        assert tick.depth is not None
        assert tick.depth.best_bid is not None
        assert tick.depth.best_ask is not None
        assert tick.depth.best_bid.price < tick.depth.best_ask.price
        assert tick.depth.spread is not None and tick.depth.spread > 0


class TestIndexPackets:
    def test_index_quote_28_bytes(self) -> None:
        packet = index_packet(NIFTY50_TOKEN, 2514200, high=2520000, low=2510000)
        assert len(packet) == 28
        tick = parse_frame(frame(packet), BASE_TIME).ticks[0]
        assert tick.is_index is True
        assert tick.last_price == Decimal("25142.00")
        assert tick.high_price == Decimal("25200.00")
        assert tick.mode is TickMode.QUOTE
        # An index has no traded volume in this feed.
        assert tick.volume is None

    def test_index_full_32_bytes_carries_a_timestamp(self) -> None:
        ts = datetime(2026, 8, 21, 4, 1, 0, tzinfo=UTC)
        packet = index_packet(NIFTY50_TOKEN, 2514200, exchange_timestamp=ts)
        assert len(packet) == 32
        tick = parse_frame(frame(packet), BASE_TIME).ticks[0]
        assert tick.exchange_timestamp == ts
        assert tick.mode is TickMode.FULL


class TestMultiplePackets:
    def test_one_frame_can_carry_mixed_modes(self) -> None:
        payload = frame(
            ltp_packet(INFY_TOKEN, 150000),
            quote_packet(RELIANCE_TOKEN, 140000),
            index_packet(NIFTY50_TOKEN, 2514200),
        )
        parsed = parse_frame(payload, BASE_TIME)
        assert len(parsed.ticks) == 3
        assert parsed.is_clean
        assert [t.mode for t in parsed.ticks] == [
            TickMode.LTP,
            TickMode.QUOTE,
            TickMode.QUOTE,
        ]


class TestMalformedFrames:
    def test_truncated_packet_is_reported_not_raised(self) -> None:
        good = frame(quote_packet(RELIANCE_TOKEN, 140000))
        parsed = parse_frame(good[:-10], BASE_TIME)
        assert parsed.ticks == ()
        assert any(f.reason == "truncated_packet" for f in parsed.findings)

    def test_truncated_length_header_is_reported(self) -> None:
        payload = struct.pack(">h", 2) + struct.pack(">h", 8) + ltp_packet(INFY_TOKEN, 1) + b"\x00"
        parsed = parse_frame(payload, BASE_TIME)
        assert len(parsed.ticks) == 1
        assert any(f.reason == "truncated_length_header" for f in parsed.findings)

    def test_unknown_packet_size_is_reported(self) -> None:
        odd = struct.pack(">i", RELIANCE_TOKEN) + b"\x00" * 12  # 16 bytes
        parsed = parse_frame(frame(odd), BASE_TIME)
        assert parsed.ticks == ()
        assert parsed.findings[0].reason == "unknown_packet_size"

    def test_packet_shorter_than_a_token_is_reported(self) -> None:
        parsed = parse_frame(frame(b"\x00\x01\x02"), BASE_TIME)
        assert parsed.findings[0].reason == "packet_too_short"

    def test_a_bad_packet_does_not_discard_the_good_ones(self) -> None:
        payload = frame(
            quote_packet(RELIANCE_TOKEN, 140000),
            b"\x00" * 16,  # unknown size
            ltp_packet(INFY_TOKEN, 150000),
        )
        parsed = parse_frame(payload, BASE_TIME)
        assert len(parsed.ticks) == 2
        assert len(parsed.findings) == 1


UINT32_MAX = 2**32 - 1
INT32_MAX = 2**31 - 1


def unsigned_quote_packet(
    token: int,
    last_price_paise: int,
    *,
    last_quantity: int = 10,
    average_price: int = 0,
    volume: int = 1000,
    total_buy: int = 500,
    total_sell: int = 400,
) -> bytes:
    """A 44-byte quote packet packed as the specification defines it: uint32.

    The shared helper packs signed, which cannot even represent the values this
    class exists to test.
    """
    return struct.pack(
        ">11I",
        token,
        last_price_paise,
        last_quantity,
        average_price or last_price_paise,
        volume,
        total_buy,
        total_sell,
        last_price_paise,
        last_price_paise,
        last_price_paise,
        last_price_paise,
    )


def unsigned_frame(*packets: bytes) -> bytes:
    out = struct.pack(">H", len(packets))
    for packet in packets:
        out += struct.pack(">H", len(packet)) + packet
    return out


class TestUnsignedDecoding:
    """Every integer in a Kite frame is unsigned.

    Read as signed, a cumulative counter past 2,147,483,647 comes back negative
    and the validator then discards the whole tick as an invalid quantity - a
    real tick lost to a decoder bug, on exactly the high-volume instruments most
    worth watching.
    """

    def test_volume_above_the_signed_boundary_stays_positive(self) -> None:
        volume = 3_000_000_000
        assert volume > INT32_MAX
        payload = unsigned_frame(unsigned_quote_packet(RELIANCE_TOKEN, 140000, volume=volume))
        parsed = parse_frame(payload, BASE_TIME)

        assert parsed.is_clean
        assert parsed.ticks[0].volume == volume

    def test_total_buy_and_sell_quantities_above_the_signed_boundary(self) -> None:
        big_buy, big_sell = 2_500_000_000, 4_000_000_000
        payload = unsigned_frame(
            unsigned_quote_packet(RELIANCE_TOKEN, 140000, total_buy=big_buy, total_sell=big_sell)
        )
        parsed = parse_frame(payload, BASE_TIME)
        tick = parsed.ticks[0]

        assert tick.total_buy_quantity == big_buy
        assert tick.total_sell_quantity == big_sell
        assert tick.total_buy_quantity > 0 and tick.total_sell_quantity > 0

    def test_last_quantity_above_the_signed_boundary(self) -> None:
        quantity = INT32_MAX + 1
        payload = unsigned_frame(
            unsigned_quote_packet(RELIANCE_TOKEN, 140000, last_quantity=quantity)
        )
        assert parse_frame(payload, BASE_TIME).ticks[0].last_quantity == quantity

    def test_maximum_uint32_decodes_without_wrapping(self) -> None:
        payload = unsigned_frame(unsigned_quote_packet(RELIANCE_TOKEN, 140000, volume=UINT32_MAX))
        assert parse_frame(payload, BASE_TIME).ticks[0].volume == UINT32_MAX

    def test_a_large_instrument_token_is_not_misread(self) -> None:
        """Token 3,000,000,001 keeps segment 1 (NSE) and stays positive."""
        token = 3_000_000_001
        assert token > INT32_MAX
        assert segment_of_token(token) == 1
        payload = unsigned_frame(unsigned_quote_packet(token, 140000))
        tick = parse_frame(payload, BASE_TIME).ticks[0]

        assert tick.instrument_token == token
        assert tick.is_index is False

    def test_depth_quantity_above_the_signed_boundary(self) -> None:
        """Depth entries are uint32 quantity, uint32 price, uint16 orders."""
        quantity = 3_500_000_000
        head = unsigned_quote_packet(RELIANCE_TOKEN, 140000)
        middle = struct.pack(">5I", 0, 0, 0, 0, 0)
        body = b""
        for _ in range(10):
            body += struct.pack(">IIHH", quantity, 139999, 7, 0)
        packet = head + middle + body
        assert len(packet) == 184

        parsed = parse_frame(unsigned_frame(packet), BASE_TIME)
        depth = parsed.ticks[0].depth

        assert depth is not None
        assert depth.bids[0].quantity == quantity
        assert depth.asks[0].quantity == quantity

    def test_a_timestamp_past_2038_is_not_negative(self) -> None:
        """uint32 epoch seconds run to 2106; signed decoding breaks in 2038."""
        beyond_2038 = 2_500_000_000
        assert beyond_2038 > INT32_MAX
        head = unsigned_quote_packet(RELIANCE_TOKEN, 140000)
        middle = struct.pack(">5I", beyond_2038, 0, 0, 0, beyond_2038)
        body = struct.pack(">IIHH", 1, 139999, 1, 0) * 10
        parsed = parse_frame(unsigned_frame(head + middle + body), BASE_TIME)
        tick = parsed.ticks[0]

        assert tick.exchange_timestamp is not None
        assert tick.exchange_timestamp.year == 2049
        assert tick.last_traded_at is not None

    def test_index_high_low_are_read_unsigned_and_price_change_is_ignored(self) -> None:
        """price_change at 24:28 is the one signed field, and is not consumed."""
        packet = struct.pack(
            ">6I", NIFTY50_TOKEN, 2_500_000, 2_600_000, 2_400_000, 2_450_000, 2_480_000
        )
        packet += struct.pack(">i", -12345)  # a falling index: legitimately negative
        parsed = parse_frame(unsigned_frame(packet), BASE_TIME)
        tick = parsed.ticks[0]

        assert tick.is_index is True
        assert tick.high_price == Decimal("26000")
        assert tick.low_price == Decimal("24000")

    def test_a_frame_declaring_many_packets_is_counted_unsigned(self) -> None:
        packets = [unsigned_quote_packet(RELIANCE_TOKEN, 140000 + n) for n in range(40)]
        parsed = parse_frame(unsigned_frame(*packets), BASE_TIME)

        assert parsed.is_clean
        assert len(parsed.ticks) == 40

    def test_a_zero_length_packet_is_still_reported_as_truncated(self) -> None:
        payload = struct.pack(">H", 1) + struct.pack(">H", 0)
        parsed = parse_frame(payload, BASE_TIME)

        assert parsed.ticks == ()
        assert parsed.findings[0].reason == "truncated_packet"

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

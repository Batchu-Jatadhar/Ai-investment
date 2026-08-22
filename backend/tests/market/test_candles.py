"""Candle aggregation: boundaries, partial vs completed, and roll-up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.time import to_ist
from app.domain.market.candles import CandleEngine, bucket_start
from app.domain.market.models import CandleInterval, CandleStatus
from tests.market.conftest import RELIANCE_TOKEN, make_tick

# 09:15:00 IST on a Friday - the NSE equity session open.
SESSION_OPEN = datetime(2026, 8, 21, 3, 45, 0, tzinfo=UTC)


def ticks_over(minutes: int, *, start: datetime = SESSION_OPEN, base: int = 1400):
    """One tick at second 10 of each of ``minutes`` consecutive minutes."""
    return [
        make_tick(
            price=str(base + index),
            at=start + timedelta(minutes=index, seconds=10),
            volume=1000 + index * 100,
        )
        for index in range(minutes)
    ]


class TestBucketing:
    def test_one_minute_bucket_floors_to_the_minute(self) -> None:
        moment = datetime(2026, 8, 21, 4, 7, 43, 512000, tzinfo=UTC)
        assert bucket_start(moment, CandleInterval.M1) == datetime(2026, 8, 21, 4, 7, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("interval", "expected_minute"),
        [(CandleInterval.M5, 5), (CandleInterval.M15, 0)],
    )
    def test_larger_buckets_floor_correctly(
        self, interval: CandleInterval, expected_minute: int
    ) -> None:
        moment = datetime(2026, 8, 21, 4, 7, 43, tzinfo=UTC)
        assert bucket_start(moment, interval).minute == expected_minute

    def test_boundaries_align_with_the_ist_session_open(self) -> None:
        """09:15 IST must start a 1m, a 5m and a 15m bucket."""
        for interval in CandleInterval:
            start = bucket_start(SESSION_OPEN, interval)
            assert start == SESSION_OPEN, interval
            assert to_ist(start).strftime("%H:%M") == "09:15"

    def test_ist_clock_boundaries_land_on_bucket_edges(self) -> None:
        for offset in (0, 5, 15, 30, 45):
            moment = SESSION_OPEN + timedelta(minutes=offset)
            assert bucket_start(moment, CandleInterval.M15) in (
                SESSION_OPEN,
                SESSION_OPEN + timedelta(minutes=15),
                SESSION_OPEN + timedelta(minutes=30),
                SESSION_OPEN + timedelta(minutes=45),
            )


class TestOneMinuteBars:
    def test_first_tick_opens_an_in_progress_bar(self) -> None:
        engine = CandleEngine()
        completed = engine.on_tick(make_tick(price="1400", at=SESSION_OPEN))
        assert completed == []
        partial = engine.partial(RELIANCE_TOKEN, CandleInterval.M1)
        assert partial is not None
        assert partial.status is CandleStatus.IN_PROGRESS
        assert partial.is_completed is False
        assert partial.open == partial.close == Decimal("1400")

    def test_ohlc_tracks_the_extremes(self) -> None:
        engine = CandleEngine()
        for price, second in (("1400", 1), ("1410", 20), ("1390", 40), ("1405", 55)):
            engine.on_tick(make_tick(price=price, at=SESSION_OPEN + timedelta(seconds=second)))
        bar = engine.partial(RELIANCE_TOKEN, CandleInterval.M1)
        assert bar is not None
        assert (bar.open, bar.high, bar.low, bar.close) == (
            Decimal("1400"),
            Decimal("1410"),
            Decimal("1390"),
            Decimal("1405"),
        )
        assert bar.tick_count == 4

    def test_a_tick_in_the_next_minute_completes_the_previous_bar(self) -> None:
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(price="1400", at=SESSION_OPEN + timedelta(seconds=5)))
        completed = engine.on_tick(
            make_tick(price="1401", at=SESSION_OPEN + timedelta(minutes=1, seconds=5))
        )
        assert len(completed) == 1
        bar = completed[0]
        assert bar.status is CandleStatus.COMPLETED
        assert bar.interval is CandleInterval.M1
        assert bar.start_at == SESSION_OPEN
        assert bar.end_at == SESSION_OPEN + timedelta(minutes=1)

    def test_completed_bar_covers_exactly_its_interval(self) -> None:
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_ticks(ticks_over(2))
        bar = engine.flush(SESSION_OPEN + timedelta(minutes=5))[0]
        assert bar.end_at - bar.start_at == timedelta(minutes=1)

    def test_volume_is_the_increase_not_the_cumulative_total(self) -> None:
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(at=SESSION_OPEN, volume=1000))
        engine.on_tick(make_tick(at=SESSION_OPEN + timedelta(seconds=30), volume=1500))
        completed = engine.on_tick(make_tick(at=SESSION_OPEN + timedelta(minutes=1), volume=1800))
        # First bar has no earlier baseline, so it measures from its first tick.
        assert completed[0].volume == 500
        engine.on_tick(make_tick(at=SESSION_OPEN + timedelta(minutes=1, seconds=30), volume=2100))
        second = engine.flush(SESSION_OPEN + timedelta(minutes=3))[0]
        assert second.volume == 2100 - 1500

    def test_flush_completes_a_bar_with_no_further_ticks(self) -> None:
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(at=SESSION_OPEN))
        assert engine.flush(SESSION_OPEN + timedelta(seconds=30)) == []
        completed = engine.flush(SESSION_OPEN + timedelta(minutes=1, seconds=1))
        assert len(completed) == 1
        assert completed[0].is_completed

    def test_out_of_order_tick_does_not_corrupt_a_closed_bar(self) -> None:
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(price="1400", at=SESSION_OPEN))
        engine.on_tick(make_tick(price="1500", at=SESSION_OPEN + timedelta(minutes=1)))
        completed = engine.on_tick(make_tick(price="9999", at=SESSION_OPEN + timedelta(seconds=30)))
        assert completed == []
        current = engine.partial(RELIANCE_TOKEN, CandleInterval.M1)
        assert current is not None
        assert current.high == Decimal("1500")


class TestDerivedIntervals:
    def test_five_minute_bar_aggregates_five_one_minute_bars(self) -> None:
        engine = CandleEngine()
        completed = engine.on_ticks(ticks_over(6))
        five = [c for c in completed if c.interval is CandleInterval.M5]
        assert len(five) == 1
        bar = five[0]
        assert bar.start_at == SESSION_OPEN
        assert bar.end_at == SESSION_OPEN + timedelta(minutes=5)
        assert bar.open == Decimal("1400")
        assert bar.close == Decimal("1404")
        assert bar.high == Decimal("1404")
        assert bar.low == Decimal("1400")

    def test_fifteen_minute_bar_aggregates_fifteen_minutes(self) -> None:
        engine = CandleEngine()
        completed = engine.on_ticks(ticks_over(16))
        fifteen = [c for c in completed if c.interval is CandleInterval.M15]
        assert len(fifteen) == 1
        bar = fifteen[0]
        assert bar.start_at == SESSION_OPEN
        assert bar.end_at == SESSION_OPEN + timedelta(minutes=15)
        assert bar.open == Decimal("1400")
        assert bar.close == Decimal("1414")

    def test_derived_volume_is_the_sum_of_the_minute_bars(self) -> None:
        engine = CandleEngine()
        completed = engine.on_ticks(ticks_over(6))
        minutes = [c for c in completed if c.interval is CandleInterval.M1]
        five = next(c for c in completed if c.interval is CandleInterval.M5)
        assert five.volume == sum(m.volume for m in minutes[:5])

    def test_derived_bars_are_in_progress_until_their_window_ends(self) -> None:
        engine = CandleEngine()
        engine.on_ticks(ticks_over(3))
        partial = engine.partial(RELIANCE_TOKEN, CandleInterval.M5)
        assert partial is not None
        assert partial.status is CandleStatus.IN_PROGRESS

    def test_all_three_intervals_complete_together_at_a_shared_boundary(self) -> None:
        engine = CandleEngine()
        completed = engine.on_ticks(ticks_over(16))
        by_interval = {c.interval for c in completed if c.start_at == SESSION_OPEN}
        assert by_interval == {
            CandleInterval.M1,
            CandleInterval.M5,
            CandleInterval.M15,
        }

    def test_one_minute_interval_is_mandatory(self) -> None:
        with pytest.raises(ValueError, match="1-minute interval is required"):
            CandleEngine(intervals=(CandleInterval.M5,))


class TestLifecycle:
    def test_close_all_completes_every_open_bar(self) -> None:
        engine = CandleEngine()
        engine.on_ticks(ticks_over(3))
        completed = engine.close_all()
        assert {c.interval for c in completed} >= {CandleInterval.M1, CandleInterval.M5}
        assert engine.open_bar_count == 0

    def test_reset_drops_open_bars_and_volume_baselines(self) -> None:
        engine = CandleEngine()
        engine.on_ticks(ticks_over(2))
        engine.reset()
        assert engine.open_bar_count == 0
        assert engine.partial(RELIANCE_TOKEN, CandleInterval.M1) is None

    def test_status_reports_engine_state(self) -> None:
        engine = CandleEngine()
        engine.on_ticks(ticks_over(2))
        status = engine.status()
        assert status["intervals"] == ["1m", "5m", "15m"]
        assert status["instruments"] == 1
        assert status["open_bars"] >= 1

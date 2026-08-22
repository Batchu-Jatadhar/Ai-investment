"""Market-data repository: persistence and the queries downstream modules use."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.market.candles import CandleEngine
from app.domain.market.models import (
    Candle,
    CandleInterval,
    CandleStatus,
    DepthLevel,
    MarketDepth,
    TickMode,
)
from app.domain.market.ports import ConnectionEvent, ConnectionEventType, DataGap
from app.domain.market.quality import DataQualityEvent, DataQualityIssue
from tests.market.conftest import RELIANCE_TOKEN, make_tick

START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


def minute_bars(count: int, token: int = RELIANCE_TOKEN) -> list[Candle]:
    """``count`` completed 1-minute bars starting at ``START``.

    Bars complete in two ways - a tick arriving in a later minute closes the
    previous bar, and ``flush`` closes the final one - so both results are
    collected.
    """
    engine = CandleEngine(intervals=(CandleInterval.M1,), source="test")
    completed = engine.on_ticks(
        make_tick(
            token=token,
            price=str(1400 + index),
            at=START + timedelta(minutes=index, seconds=5),
            volume=1000 + index * 10,
        )
        for index in range(count)
    )
    completed.extend(engine.flush(START + timedelta(minutes=count + 1)))
    return completed


class TestInstrumentQueries:
    def test_round_trip_by_token_and_symbol(self, repository, instruments) -> None:  # noqa: ANN001
        repository.replace_instruments(instruments, START)
        found = repository.get_instrument_by_token(RELIANCE_TOKEN)
        assert found is not None
        assert found.tradingsymbol == "RELIANCE"
        assert found.tick_size == Decimal("0.050000")
        assert repository.get_instrument_by_symbol("NSE", "INFY") is not None

    def test_index_flag_survives_the_round_trip(self, repository, instruments) -> None:  # noqa: ANN001
        repository.replace_instruments(instruments, START)
        nifty = repository.get_instrument_by_symbol("NSE", "NIFTY 50")
        assert nifty is not None
        assert nifty.is_index is True

    def test_retrieved_at_is_timezone_aware(self, repository) -> None:  # noqa: ANN001
        from tests.market.conftest import make_instrument

        stamped = [make_instrument(RELIANCE_TOKEN, "RELIANCE", retrieved_at=START)]
        repository.replace_instruments(stamped, START)
        retrieved = repository.instruments_retrieved_at()
        assert retrieved is not None
        assert retrieved.tzinfo is not None
        assert retrieved == START

    def test_missing_instrument_returns_none(self, repository) -> None:  # noqa: ANN001
        assert repository.get_instrument_by_token(1) is None
        assert repository.get_instrument_by_symbol("NSE", "NOPE") is None


class TestTickQueries:
    def test_save_and_read_back_the_latest(self, repository) -> None:  # noqa: ANN001
        ticks = [
            make_tick(price="1400", at=START),
            make_tick(price="1405", at=START + timedelta(seconds=30)),
        ]
        assert repository.save_ticks(ticks) == 2
        latest = repository.latest_tick(RELIANCE_TOKEN)
        assert latest is not None
        assert latest.last_price == Decimal("1405.000000")
        assert latest.event_time.tzinfo is not None

    def test_range_query_is_half_open(self, repository) -> None:  # noqa: ANN001
        repository.save_ticks(
            [make_tick(at=START + timedelta(minutes=m), price=str(1400 + m)) for m in range(5)]
        )
        window = repository.ticks_in_range(
            RELIANCE_TOKEN, START + timedelta(minutes=1), START + timedelta(minutes=3)
        )
        assert [t.last_price for t in window] == [
            Decimal("1401.000000"),
            Decimal("1402.000000"),
        ]

    def test_answers_what_did_we_receive_for_x_during_y(self, repository) -> None:  # noqa: ANN001
        repository.save_ticks([make_tick(at=START + timedelta(seconds=s * 10)) for s in range(6)])
        got = repository.ticks_in_range(RELIANCE_TOKEN, START, START + timedelta(minutes=1))
        assert len(got) == 6

    def test_depth_survives_the_round_trip(self, repository) -> None:  # noqa: ANN001
        from dataclasses import replace

        depth = MarketDepth(
            bids=(DepthLevel(Decimal("1399.95"), 100, 2),),
            asks=(DepthLevel(Decimal("1400.05"), 150, 3),),
        )
        repository.save_ticks([replace(make_tick(), depth=depth)])
        latest = repository.latest_tick(RELIANCE_TOKEN)
        assert latest is not None and latest.depth is not None
        assert latest.depth.best_bid.quantity == 100
        assert latest.depth.best_ask.price == Decimal("1400.05")

    def test_mode_is_preserved(self, repository) -> None:  # noqa: ANN001
        repository.save_ticks([make_tick(mode=TickMode.LTP)])
        latest = repository.latest_tick(RELIANCE_TOKEN)
        assert latest is not None and latest.mode is TickMode.LTP

    def test_saving_nothing_is_free(self, repository) -> None:  # noqa: ANN001
        assert repository.save_ticks([]) == 0

    def test_pruning_respects_the_cutoff(self, repository) -> None:  # noqa: ANN001
        repository.save_ticks([make_tick(at=START + timedelta(minutes=m)) for m in range(4)])
        removed = repository.prune_ticks_before(START + timedelta(minutes=2))
        assert removed == 2
        assert (
            len(repository.ticks_in_range(RELIANCE_TOKEN, START, START + timedelta(hours=1))) == 2
        )


class TestCandleQueries:
    def test_completed_candles_are_stored(self, repository) -> None:  # noqa: ANN001
        bars = minute_bars(3)
        assert repository.save_candles(bars) == 3
        latest = repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M1)
        assert latest is not None
        assert latest.status is CandleStatus.COMPLETED
        assert latest.start_at.tzinfo is not None

    def test_in_progress_candles_are_never_written(self, repository) -> None:  # noqa: ANN001
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(at=START))
        partial = engine.partial(RELIANCE_TOKEN, CandleInterval.M1)
        assert partial is not None and not partial.is_completed
        assert repository.save_candles([partial]) == 0
        assert repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M1) is None

    def test_re_saving_the_same_bucket_is_idempotent(self, repository) -> None:  # noqa: ANN001
        bars = minute_bars(2)
        repository.save_candles(bars)
        assert repository.save_candles(bars) == 0
        assert len(repository.recent_candles(RELIANCE_TOKEN, CandleInterval.M1, 10)) == 2

    def test_range_query_returns_ascending_bars(self, repository) -> None:  # noqa: ANN001
        repository.save_candles(minute_bars(5))
        bars = repository.candles_in_range(
            RELIANCE_TOKEN, CandleInterval.M1, START, START + timedelta(minutes=3)
        )
        assert len(bars) == 3
        assert [b.start_at for b in bars] == sorted(b.start_at for b in bars)

    def test_recent_candles_are_ordered_oldest_first(self, repository) -> None:  # noqa: ANN001
        repository.save_candles(minute_bars(5))
        bars = repository.recent_candles(RELIANCE_TOKEN, CandleInterval.M1, 3)
        assert len(bars) == 3
        assert bars[0].start_at < bars[-1].start_at
        assert bars[-1].start_at == START + timedelta(minutes=4)

    def test_intervals_are_stored_separately(self, repository) -> None:  # noqa: ANN001
        engine = CandleEngine(source="test")
        completed = engine.on_ticks(
            make_tick(price=str(1400 + i), at=START + timedelta(minutes=i, seconds=5))
            for i in range(6)
        )
        repository.save_candles(completed)
        assert repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M1)
        assert repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M5)
        assert repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M15) is None

    def test_prices_keep_their_precision(self, repository) -> None:  # noqa: ANN001
        engine = CandleEngine(intervals=(CandleInterval.M1,))
        engine.on_tick(make_tick(price="1400.05", at=START))
        repository.save_candles(engine.flush(START + timedelta(minutes=2)))
        bar = repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M1)
        assert bar is not None
        assert bar.open == Decimal("1400.050000")


class TestOperationalEvents:
    def test_connection_events_are_recorded(self, repository) -> None:  # noqa: ANN001
        repository.record_connection_event(
            ConnectionEvent(
                event_type=ConnectionEventType.CONNECTED,
                provider="zerodha",
                occurred_at=START,
                detail={"attempt": 1},
            )
        )
        recent = repository.recent_connection_events()
        assert len(recent) == 1
        assert recent[0].event_type == "connected"

    def test_data_gaps_are_recorded_with_a_duration(self, repository) -> None:  # noqa: ANN001
        repository.record_data_gap(
            DataGap(
                provider="zerodha",
                started_at=START,
                ended_at=START + timedelta(seconds=12, milliseconds=500),
                reason="reconnect",
                instrument_tokens=(RELIANCE_TOKEN,),
            )
        )
        assert repository.data_gap_count() == 1

    def test_quality_events_are_recorded(self, repository) -> None:  # noqa: ANN001
        written = repository.record_quality_events(
            [
                DataQualityEvent(
                    issue=DataQualityIssue.DUPLICATE_TICK,
                    occurred_at=START,
                    instrument_token=RELIANCE_TOKEN,
                    provider="zerodha",
                    detail={"note": "x"},
                )
            ]
        )
        assert written == 1

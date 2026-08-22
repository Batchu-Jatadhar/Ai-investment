"""End-to-end pipeline over the replay provider.

Provider -> validation -> candle engine -> repository, exercised without any
network. The replay provider is explicitly not live data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.adapters.replay.provider import (
    FakeMarketDataProvider,
    ReplayMarketDataProvider,
)
from app.core.time import FixedClock
from app.domain.market.models import CandleInterval, TickMode
from app.domain.market.ports import (
    DataGap,
    ProviderState,
    TickBatch,
)
from app.services.instrument_master import InstrumentMaster
from app.services.market_data_service import (
    MarketDataService,
    get_active_service,
    set_active_service,
)
from tests.market.conftest import RELIANCE_TOKEN, UNKNOWN_TOKEN, make_tick

START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


def build(repository, instruments, events=None, *, clock=None):  # noqa: ANN001
    repository.replace_instruments(instruments, START)
    master = InstrumentMaster(repository, None, clock=clock or FixedClock(START))
    master.load_from_repository()
    provider = ReplayMarketDataProvider(events or [], clock=clock or FixedClock(START))
    service = MarketDataService(
        provider,
        repository,
        master,
        clock=clock or FixedClock(START),
        persist_ticks=True,
    )
    return service, provider


def tick_batch(*ticks):  # noqa: ANN001, ANN201
    return TickBatch(ticks=tuple(ticks), received_at=ticks[0].received_at, provider="replay")


class TestReplayProviderIsNotLive:
    def test_it_is_named_honestly(self) -> None:
        assert ReplayMarketDataProvider.name == "replay"
        assert FakeMarketDataProvider is ReplayMarketDataProvider

    async def test_its_ticks_are_marked_replay(self, repository, instruments) -> None:  # noqa: ANN001
        service, _ = build(repository, instruments, [tick_batch(make_tick(at=START))])
        await service.run()
        stored = repository.latest_tick(RELIANCE_TOKEN)
        assert stored is not None
        assert service.health()["provider"]["provider"] == "replay"


class TestPipeline:
    async def test_ticks_are_validated_stored_and_aggregated(self, repository, instruments) -> None:  # noqa: ANN001
        ticks = [
            make_tick(
                price=str(1400 + i),
                at=START + timedelta(minutes=i, seconds=5),
                volume=1000 + i * 10,
            )
            for i in range(6)
        ]
        service, _ = build(repository, instruments, [tick_batch(t) for t in ticks])
        await service.run()

        assert service.ticks_accepted == 6
        assert service.ticks_rejected == 0
        assert (
            len(repository.ticks_in_range(RELIANCE_TOKEN, START, START + timedelta(hours=1))) == 6
        )

        minute_bars = repository.recent_candles(RELIANCE_TOKEN, CandleInterval.M1, 10)
        assert len(minute_bars) == 6  # the last one is closed by the finaliser

        # Six minutes spans two 5-minute buckets: 03:45-03:50 and 03:50-03:55.
        five_bars = repository.recent_candles(RELIANCE_TOKEN, CandleInterval.M5, 10)
        assert len(five_bars) == 2
        assert five_bars[0].start_at == START
        assert five_bars[0].open == minute_bars[0].open
        assert five_bars[0].close == minute_bars[4].close
        assert five_bars[1].open == minute_bars[5].open

    async def test_bad_ticks_are_rejected_and_recorded(self, repository, instruments) -> None:  # noqa: ANN001
        good = make_tick(at=START)
        unknown = make_tick(token=UNKNOWN_TOKEN, at=START, symbol=None)
        duplicate = make_tick(at=START)
        service, _ = build(repository, instruments, [tick_batch(good, unknown, duplicate)])
        await service.run()
        assert service.ticks_accepted == 1
        assert service.ticks_rejected == 2
        counters = service.validator.counters
        assert counters.by_issue["unknown_instrument"] == 1
        assert counters.by_issue["duplicate_tick"] == 1

    async def test_ticks_are_enriched_from_the_instrument_master(
        self, repository, instruments
    ) -> None:  # noqa: ANN001
        bare = make_tick(at=START, symbol=None)
        from dataclasses import replace

        service, _ = build(repository, instruments, [tick_batch(replace(bare, exchange=None))])
        await service.run()
        stored = repository.latest_tick(RELIANCE_TOKEN)
        assert stored is not None
        assert stored.tradingsymbol == "RELIANCE"
        assert stored.exchange == "NSE"

    async def test_persisting_ticks_can_be_switched_off(self, repository, instruments) -> None:  # noqa: ANN001
        service, _ = build(repository, instruments, [tick_batch(make_tick(at=START))])
        service._persist_ticks = False  # noqa: SLF001
        await service.run()
        assert repository.latest_tick(RELIANCE_TOKEN) is None
        # Candles are still produced.
        assert repository.latest_completed_candle(RELIANCE_TOKEN, CandleInterval.M1)


class TestGapHandling:
    async def test_a_gap_is_recorded_and_resets_in_flight_bars(
        self, repository, instruments
    ) -> None:  # noqa: ANN001
        gap = DataGap(
            provider="replay",
            started_at=START + timedelta(minutes=1),
            ended_at=START + timedelta(minutes=2),
            reason="reconnect",
            instrument_tokens=(RELIANCE_TOKEN,),
        )
        events = [
            tick_batch(make_tick(at=START, price="1400")),
            gap,
            tick_batch(make_tick(at=START + timedelta(minutes=2), price="1500")),
        ]
        service, _ = build(repository, instruments, events)
        await service.run()

        assert service.gaps_recorded == 1
        assert repository.data_gap_count() == 1
        bars = repository.recent_candles(RELIANCE_TOKEN, CandleInterval.M1, 10)
        # The bar in progress when the gap opened is discarded, not stitched
        # across the gap.
        assert all(b.start_at != START for b in bars)

    async def test_connection_events_are_persisted(self, repository, instruments) -> None:  # noqa: ANN001
        service, _ = build(repository, instruments, [])
        await service.run()
        kinds = {e.event_type for e in repository.recent_connection_events()}
        assert "connected" in kinds
        assert "disconnected" in kinds


class TestSubscription:
    async def test_subscribes_to_the_resolved_universe(self, repository, instruments) -> None:  # noqa: ANN001
        service, provider = build(repository, instruments, [])
        tokens = await service.subscribe_universe("NSE:RELIANCE,NSE:NIFTY 50")
        assert len(tokens) == 2
        assert provider.subscribe_calls[0][1] is TickMode.FULL
        assert set(provider.subscribed_tokens) == set(tokens)

    async def test_unresolvable_universe_subscribes_to_nothing(
        self, repository, instruments
    ) -> None:  # noqa: ANN001
        service, provider = build(repository, instruments, [])
        assert await service.subscribe_universe("NSE:NOSUCH") == ()
        assert provider.subscribed_tokens == frozenset()


class TestHealthSnapshot:
    async def test_health_reports_real_counters(self, repository, instruments) -> None:  # noqa: ANN001
        service, _ = build(repository, instruments, [tick_batch(make_tick(at=START))])
        await service.run()
        health = service.health()
        assert health["running"] is False
        assert health["stream"]["ticks_accepted"] == 1
        assert health["candles"]["completed_total"] >= 1
        assert health["instruments"]["count"] == 4
        assert health["session"]["state"] == "open"  # 09:15 IST

    async def test_is_live_is_false_after_the_stream_ends(self, repository, instruments) -> None:  # noqa: ANN001
        service, provider = build(repository, instruments, [])
        await service.run()
        assert service.is_live() is False
        assert provider.state is ProviderState.STOPPED


class TestActiveServiceRegistry:
    def test_register_and_clear(self, repository, instruments) -> None:  # noqa: ANN001
        service, _ = build(repository, instruments, [])
        assert get_active_service() is None
        set_active_service(service)
        try:
            assert get_active_service() is service
        finally:
            set_active_service(None)
        assert get_active_service() is None

"""Instrument master: refresh, validation, lookup, staleness, universe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.time import FixedClock
from app.domain.market.models import Instrument
from app.domain.market.universe import (
    UniverseEntry,
    UniverseSpecError,
    parse_universe,
)
from app.services.instrument_master import (
    InstrumentMaster,
    InstrumentMasterError,
    validate_instruments,
)
from tests.market.conftest import (
    BANKNIFTY_TOKEN,
    NIFTY50_TOKEN,
    RELIANCE_TOKEN,
    UNKNOWN_TOKEN,
    make_instrument,
)

NOW = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)


class StubSource:
    name = "stub"

    def __init__(self, instruments: list[Instrument]) -> None:
        self._instruments = instruments
        self.calls = 0

    async def fetch_instruments(self, exchange: str | None = None) -> list[Instrument]:
        self.calls += 1
        return list(self._instruments)


def master(repository, instruments=None, *, clock=None, max_age=timedelta(hours=24)):  # noqa: ANN001
    source = StubSource(instruments) if instruments is not None else None
    return InstrumentMaster(repository, source, clock=clock or FixedClock(NOW), max_age=max_age)


class TestValidation:
    def test_clean_dump_is_usable(self, instruments) -> None:  # noqa: ANN001
        report = validate_instruments(instruments)
        assert report.is_usable
        assert report.total == 4
        assert report.duplicate_symbols == ()

    def test_duplicate_symbol_makes_a_dump_unusable(self) -> None:
        dupes = [
            make_instrument(1 << 8 | 1, "RELIANCE"),
            make_instrument(2 << 8 | 1, "RELIANCE"),
        ]
        report = validate_instruments(dupes)
        assert report.is_usable is False
        assert "NSE:RELIANCE" in report.duplicate_symbols

    def test_duplicate_token_is_reported(self) -> None:
        report = validate_instruments(
            [make_instrument(RELIANCE_TOKEN, "A"), make_instrument(RELIANCE_TOKEN, "B")]
        )
        assert RELIANCE_TOKEN in report.duplicate_tokens

    def test_zero_lot_size_flagged_only_for_tradable_instruments(self) -> None:
        report = validate_instruments(
            [
                make_instrument(RELIANCE_TOKEN, "RELIANCE", lot_size=0),
                make_instrument(NIFTY50_TOKEN, "NIFTY 50", segment="INDICES", lot_size=0),
            ]
        )
        assert report.zero_lot_size == ("NSE:RELIANCE",)


class TestRefresh:
    async def test_refresh_stores_and_indexes(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        result = await m.refresh()
        assert result.stored == 4
        assert result.retrieved_at == NOW
        assert repository.instrument_count() == 4
        assert m.by_symbol("NSE", "RELIANCE") is not None

    async def test_refresh_replaces_rather_than_merges(self, repository) -> None:  # noqa: ANN001
        await master(repository, [make_instrument(RELIANCE_TOKEN, "RELIANCE")]).refresh()
        await master(repository, [make_instrument(1 << 8 | 1, "TCS")]).refresh()
        assert repository.instrument_count() == 1
        assert repository.get_instrument_by_symbol("NSE", "RELIANCE") is None

    async def test_unusable_dump_is_rejected(self, repository) -> None:  # noqa: ANN001
        bad = [make_instrument(1 << 8 | 1, "X"), make_instrument(2 << 8 | 1, "X")]
        with pytest.raises(InstrumentMasterError, match="unusable"):
            await master(repository, bad).refresh()
        assert repository.instrument_count() == 0

    async def test_refresh_without_a_source_is_an_error(self, repository) -> None:  # noqa: ANN001
        with pytest.raises(InstrumentMasterError, match="no instrument source"):
            await master(repository).refresh()


class TestLookup:
    async def test_lookup_by_token(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        found = m.by_token(RELIANCE_TOKEN)
        assert found is not None and found.tradingsymbol == "RELIANCE"

    async def test_lookup_by_symbol_is_case_insensitive(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        assert m.by_symbol("nse", "reliance") is not None
        assert m.by_symbol(" NSE ", " RELIANCE ") is not None

    async def test_unknown_token_returns_none(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        assert m.by_token(UNKNOWN_TOKEN) is None

    async def test_unknown_symbol_returns_none(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        assert m.by_symbol("NSE", "NOSUCHSYMBOL") is None

    async def test_lookup_falls_back_to_the_repository(self, repository, instruments) -> None:  # noqa: ANN001
        await master(repository, instruments).refresh()
        cold = master(repository)  # empty cache, no source
        assert cold.by_token(RELIANCE_TOKEN) is not None
        assert cold.by_symbol("NSE", "INFY") is not None

    async def test_load_from_repository_populates_the_cache(self, repository, instruments) -> None:  # noqa: ANN001
        await master(repository, instruments).refresh()
        cold = master(repository)
        assert cold.load_from_repository() == 4
        assert cold.count == 4


class TestStaleness:
    async def test_fresh_master_is_not_stale(self, repository, instruments) -> None:  # noqa: ANN001
        clock = FixedClock(NOW)
        m = master(repository, instruments, clock=clock)
        await m.refresh()
        assert m.is_stale() is False

    async def test_master_goes_stale_after_the_max_age(self, repository, instruments) -> None:  # noqa: ANN001
        clock = FixedClock(NOW)
        m = master(repository, instruments, clock=clock, max_age=timedelta(hours=24))
        await m.refresh()
        clock.advance(timedelta(hours=25))
        assert m.is_stale() is True
        assert m.status()["stale"] is True

    def test_absent_master_counts_as_stale(self, repository) -> None:  # noqa: ANN001
        m = master(repository)
        assert m.retrieved_at is None
        assert m.is_stale() is True

    async def test_status_reports_age(self, repository, instruments) -> None:  # noqa: ANN001
        clock = FixedClock(NOW)
        m = master(repository, instruments, clock=clock)
        await m.refresh()
        clock.advance(timedelta(minutes=30))
        status = m.status()
        assert status["age_seconds"] == 1800.0
        assert status["count"] == 4


class TestUniverse:
    def test_parses_exchange_and_symbol(self) -> None:
        entries = parse_universe("NSE:RELIANCE, NSE:INFY")
        assert entries == (
            UniverseEntry("NSE", "RELIANCE"),
            UniverseEntry("NSE", "INFY"),
        )

    def test_symbols_with_spaces_are_supported(self) -> None:
        assert parse_universe("NSE:NIFTY 50")[0].tradingsymbol == "NIFTY 50"

    def test_duplicates_are_collapsed(self) -> None:
        assert len(parse_universe("NSE:RELIANCE,NSE:RELIANCE")) == 1

    def test_missing_exchange_is_rejected(self) -> None:
        with pytest.raises(UniverseSpecError, match="EXCHANGE:TRADINGSYMBOL"):
            parse_universe("RELIANCE")

    def test_blank_entries_are_ignored(self) -> None:
        assert len(parse_universe("NSE:RELIANCE,, ,")) == 1

    async def test_resolution_maps_symbols_to_tokens(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        resolution = m.resolve("NSE:RELIANCE,NSE:INFY")
        assert resolution.is_complete
        assert set(resolution.tokens) == {RELIANCE_TOKEN, instruments[1].instrument_token}

    async def test_unknown_entries_are_reported_not_dropped(self, repository, instruments) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        resolution = m.resolve("NSE:RELIANCE,NSE:NOSUCH")
        assert resolution.is_complete is False
        assert resolution.unresolved[0].key == "NSE:NOSUCH"
        assert len(resolution.tokens) == 1

    async def test_indices_resolve_but_are_flagged_not_tradable(
        self, repository, instruments
    ) -> None:  # noqa: ANN001
        m = master(repository, instruments)
        await m.refresh()
        resolution = m.resolve("NSE:NIFTY 50,NSE:NIFTY BANK,NSE:RELIANCE")
        assert {i.instrument_token for i in resolution.indices} == {
            NIFTY50_TOKEN,
            BANKNIFTY_TOKEN,
        }
        assert [i.tradingsymbol for i in resolution.tradable] == ["RELIANCE"]
        # They are still subscribed for data.
        assert len(resolution.tokens) == 3

    async def test_no_instrument_token_is_hard_coded(self, repository, instruments) -> None:  # noqa: ANN001
        """Tokens must come from the master, never from configuration."""
        m = master(repository, instruments)
        await m.refresh()
        resolution = m.resolve("NSE:RELIANCE")
        assert resolution.tokens == (RELIANCE_TOKEN,)
        assert resolution.resolved[0].source == "test"

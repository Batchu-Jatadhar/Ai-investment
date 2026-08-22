"""Market-data health and the read-only query endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.domain.market.candles import CandleEngine
from app.services.market_data_service import set_active_service
from tests.conftest import build_settings
from tests.market.conftest import RELIANCE_TOKEN, make_tick

START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)


@pytest.fixture
def seeded(repository, instruments):  # noqa: ANN001, ANN201
    repository.replace_instruments(instruments, START)
    engine = CandleEngine(source="test")
    completed = engine.on_ticks(
        make_tick(price=str(1400 + i), at=START + timedelta(minutes=i, seconds=5)) for i in range(7)
    )
    completed.extend(engine.close_all())
    repository.save_candles(completed)
    repository.save_ticks([make_tick(price="1406", at=START + timedelta(minutes=6))])
    return repository


class TestMarketDataHealth:
    def test_reports_not_running_when_no_streamer_is_present(self, client: TestClient) -> None:
        response = client.get("/health/market-data")
        assert response.status_code == 503
        body = response.json()
        assert body["running_in_this_process"] is False
        assert body["zerodha"]["connected"] is False
        assert body["stream"]["subscribed_instruments"] == 0
        assert "aitrade-marketdata" in (body["note"] or "")

    def test_unconfigured_credentials_are_reported_honestly(self, client: TestClient) -> None:
        body = client.get("/health/market-data").json()
        assert body["status"] == "not_configured"
        assert body["zerodha"]["configured"] is False
        assert body["zerodha"]["state"] == "ZERODHA_NOT_CONFIGURED"
        assert body["zerodha"]["authenticated"] is False

    def test_instrument_freshness_comes_from_the_database(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get("/health/market-data").json()
        assert body["instruments"]["count"] == 4
        assert body["instruments"]["retrieved_at"] is not None

    def test_absent_instrument_master_is_marked_stale(self, client: TestClient) -> None:
        body = client.get("/health/market-data").json()
        assert body["instruments"]["stale"] is True

    def test_session_state_is_included(self, client: TestClient) -> None:
        body = client.get("/health/market-data").json()
        assert "state" in body["session"]
        assert body["session"]["window"] == "NSE_EQUITY"

    def test_reports_a_running_service(self, client: TestClient, repository, instruments) -> None:  # noqa: ANN001
        from tests.market.test_market_data_service import build, tick_batch

        service, _ = build(repository, instruments, [tick_batch(make_tick(at=START))])
        set_active_service(service)
        try:
            body = client.get("/health/market-data").json()
        finally:
            set_active_service(None)
        assert body["running_in_this_process"] is False  # not started yet
        assert body["provider"] == "replay"
        assert body["stream"]["ticks_accepted"] == 0

    def test_never_leaks_credentials(self, client: TestClient) -> None:
        text = client.get("/health/market-data").text.lower()
        for forbidden in ("api_key", "access_token", "secret", "password"):
            assert forbidden not in text


class TestInstrumentEndpoints:
    def test_lookup_by_symbol(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get("/market-data/instruments/NSE/RELIANCE").json()
        assert body["instrument_token"] == RELIANCE_TOKEN
        assert body["is_index"] is False
        assert body["tick_size"] == "0.050000"

    def test_index_is_flagged(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get("/market-data/instruments/NSE/NIFTY 50").json()
        assert body["is_index"] is True

    def test_lookup_by_token(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get(f"/market-data/instruments/by-token/{RELIANCE_TOKEN}").json()
        assert body["tradingsymbol"] == "RELIANCE"

    def test_unknown_symbol_is_a_problem_response(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        response = client.get("/market-data/instruments/NSE/NOSUCH")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "not_found"


class TestCandleEndpoints:
    def test_recent_candles(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get(
            f"/market-data/candles/{RELIANCE_TOKEN}", params={"interval": "1m", "limit": 3}
        ).json()
        assert len(body) == 3
        assert body[0]["status"] == "completed"
        assert body[0]["start_at"] < body[-1]["start_at"]

    def test_interval_selects_the_series(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        five = client.get(
            f"/market-data/candles/{RELIANCE_TOKEN}", params={"interval": "5m"}
        ).json()
        assert five
        assert all(c["interval"] == "5m" for c in five)

    def test_range_query(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get(
            f"/market-data/candles/{RELIANCE_TOKEN}",
            params={
                "interval": "1m",
                "start": START.isoformat(),
                "end": (START + timedelta(minutes=3)).isoformat(),
            },
        ).json()
        assert len(body) == 3

    def test_latest_candle(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get(
            f"/market-data/candles/{RELIANCE_TOKEN}/latest", params={"interval": "1m"}
        ).json()
        assert body["interval"] == "1m"
        assert body["status"] == "completed"

    def test_no_candles_is_a_404(self, client: TestClient) -> None:
        assert client.get("/market-data/candles/12345/latest").status_code == 404

    def test_invalid_interval_is_rejected(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        response = client.get(f"/market-data/candles/{RELIANCE_TOKEN}", params={"interval": "3m"})
        assert response.status_code == 422


class TestTickEndpoint:
    def test_latest_tick(self, client: TestClient, seeded) -> None:  # noqa: ANN001
        body = client.get(f"/market-data/ticks/{RELIANCE_TOKEN}/latest").json()
        assert body["last_price"] == "1406.000000"
        assert body["instrument_token"] == RELIANCE_TOKEN

    def test_no_ticks_is_a_404(self, client: TestClient) -> None:
        assert client.get("/market-data/ticks/999/latest").status_code == 404


class TestMarketDataConfiguration:
    def test_defaults_are_sane(self) -> None:
        settings = build_settings()
        assert settings.market_data_provider.value == "zerodha"
        assert settings.market_data_mode.value == "full"
        assert "1m" in settings.interval_names
        assert settings.zerodha_configured is False

    def test_universe_is_parsed_from_configuration(self) -> None:
        settings = build_settings(market_data_universe="NSE:RELIANCE, NSE:INFY")
        assert settings.universe_entries == ("NSE:RELIANCE", "NSE:INFY")

    def test_malformed_universe_is_rejected_at_load(self) -> None:
        from app.config.settings import ConfigurationError

        with pytest.raises(ConfigurationError, match="MARKET_DATA_UNIVERSE"):
            build_settings(market_data_universe="RELIANCE")

    def test_unsupported_interval_is_rejected(self) -> None:
        from app.config.settings import ConfigurationError

        with pytest.raises(ConfigurationError, match="MARKET_DATA_INTERVALS"):
            build_settings(market_data_intervals="1m,3m")

    def test_one_minute_interval_is_mandatory(self) -> None:
        from app.config.settings import ConfigurationError

        with pytest.raises(ConfigurationError, match="must include 1m"):
            build_settings(market_data_intervals="5m,15m")

    def test_zerodha_configured_requires_key_and_token(self) -> None:
        assert build_settings(zerodha_api_key="k").zerodha_configured is False
        assert (
            build_settings(zerodha_api_key="k", zerodha_access_token="t").zerodha_configured is True
        )

    def test_secrets_are_read_only_through_the_accessor(self) -> None:
        settings = build_settings(
            zerodha_api_key="unmistakable-key-value", zerodha_access_token="tok"
        )
        assert settings.zerodha_secret("api_key") == "unmistakable-key-value"
        assert settings.zerodha_secret("api_secret") is None
        assert "unmistakable-key-value" not in str(settings.safe_dump())

    def test_application_starts_without_broker_credentials(self, client: TestClient) -> None:
        """Phase 1 must not require credentials to run locally."""
        assert client.get("/health").status_code == 200
        assert client.get("/health/market-data").json()["zerodha"]["configured"] is False

"""Trading-mode safety.

Architecture v0.3 §19.1: the system defaults to PAPER, and LIVE must not be
reachable by accident. These tests are the executable form of that rule.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config.settings import (
    LIVE_TRADING_IMPLEMENTED,
    ConfigurationError,
    Settings,
    TradingMode,
)
from tests.conftest import build_settings

FULL_LIVE_CONFIG = {
    "trading_mode": "live",
    "zerodha_api_key": "k",
    "zerodha_api_secret": "s",
    "zerodha_access_token": "t",
    "compliance_algo_id": "ALGO-1",
    "compliance_classification": "white_box",
    "compliance_registered_ip": "203.0.113.10",
    "compliance_confirmation_ref": "ticket-1",
    "live_trading_armed": True,
    "live_capital_ceiling_inr": 10_000,
}


class TestDefaultMode:
    def test_paper_is_the_default(self) -> None:
        assert Settings(_env_file=None).trading_mode is TradingMode.PAPER  # type: ignore[call-arg]

    def test_default_holds_with_an_empty_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("TRADING_MODE", "LIVE_TRADING_ARMED", "LIVE_CAPITAL_CEILING_INR"):
            monkeypatch.delenv(var, raising=False)
        assert Settings(_env_file=None).trading_mode is TradingMode.PAPER  # type: ignore[call-arg]

    def test_backtest_and_paper_are_permitted(self) -> None:
        assert build_settings(trading_mode="backtest").trading_mode is TradingMode.BACKTEST
        assert build_settings(trading_mode="paper").trading_mode is TradingMode.PAPER


class TestLiveIsBlocked:
    def test_live_phase_gate_is_closed(self) -> None:
        assert LIVE_TRADING_IMPLEMENTED is False

    def test_live_rejected_when_all_configuration_absent(self) -> None:
        with pytest.raises(ConfigurationError) as exc:
            build_settings(trading_mode="live")
        message = str(exc.value)
        assert "requires configuration that is absent" in message
        for required in (
            "ZERODHA_API_KEY",
            "ZERODHA_API_SECRET",
            "ZERODHA_ACCESS_TOKEN",
            "COMPLIANCE_ALGO_ID",
            "LIVE_TRADING_ARMED",
            "LIVE_CAPITAL_CEILING_INR",
        ):
            assert required in message

    @pytest.mark.parametrize(
        "omitted",
        [
            "zerodha_api_key",
            "zerodha_api_secret",
            "zerodha_access_token",
            "compliance_algo_id",
            "compliance_classification",
            "compliance_registered_ip",
            "compliance_confirmation_ref",
        ],
    )
    def test_live_rejected_when_any_single_field_is_absent(self, omitted: str) -> None:
        config = {k: v for k, v in FULL_LIVE_CONFIG.items() if k != omitted}
        with pytest.raises(ConfigurationError) as exc:
            build_settings(**config)
        assert omitted.upper() in str(exc.value)

    def test_live_rejected_when_not_armed(self) -> None:
        config = FULL_LIVE_CONFIG | {"live_trading_armed": False}
        with pytest.raises(ConfigurationError, match="LIVE_TRADING_ARMED"):
            build_settings(**config)

    def test_live_rejected_when_capital_ceiling_is_zero(self) -> None:
        config = FULL_LIVE_CONFIG | {"live_capital_ceiling_inr": 0}
        with pytest.raises(ConfigurationError, match="LIVE_CAPITAL_CEILING_INR"):
            build_settings(**config)

    def test_live_still_rejected_when_fully_configured(self) -> None:
        """The phase gate is the last barrier: complete config is not enough."""
        with pytest.raises(ConfigurationError, match="not implemented"):
            build_settings(**FULL_LIVE_CONFIG)


class TestNoExecutionSurface:
    """No route may exist that could place, modify or cancel an order."""

    FORBIDDEN = (
        "order",
        "trade",
        "execute",
        "execution",
        "position",
        "broker",
        "zerodha",
        "webhook",
        "tradingview",
        "kill",
        "flatten",
    )

    @staticmethod
    def _published_paths(app: FastAPI) -> set[str]:
        """The real HTTP surface, as published in the OpenAPI schema."""
        return set(app.openapi()["paths"])

    def test_no_trading_routes_are_registered(self, app: FastAPI) -> None:
        paths = self._published_paths(app)
        assert paths, "the API must publish at least one path"
        offending = sorted(p for p in paths if any(w in p.lower() for w in self.FORBIDDEN))
        assert offending == [], f"execution-capable routes must not exist: {offending}"

    def test_no_mutating_methods_are_exposed(self, app: FastAPI) -> None:
        """Phase 0 is read-only. Nothing may accept a write."""
        schema = app.openapi()["paths"]
        mutating = sorted(
            f"{method.upper()} {path}"
            for path, ops in schema.items()
            for method in ops
            if method.lower() in {"post", "put", "patch", "delete"}
        )
        assert mutating == [], f"no write endpoints are permitted yet: {mutating}"

    def test_only_health_endpoints_are_exposed(self, app: FastAPI) -> None:
        assert self._published_paths(app) == {"/health", "/health/db"}

    def test_health_reports_live_trading_not_implemented(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["live_trading_implemented"] is False
        assert body["trading_mode"] == "paper"

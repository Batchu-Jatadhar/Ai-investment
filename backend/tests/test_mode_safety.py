"""Trading-mode safety.

Architecture v0.3 §19.1: the system defaults to PAPER, and LIVE must not be
reachable by accident. These tests are the executable form of that rule.
"""

from __future__ import annotations

import ast
import pathlib

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

    def test_only_read_only_surfaces_are_exposed(self, app: FastAPI) -> None:
        """Health plus read-only market-data queries. Nothing else."""
        assert self._published_paths(app) == {
            "/health",
            "/health/db",
            "/health/market-data",
            "/market-data/instruments/{exchange}/{tradingsymbol}",
            "/market-data/instruments/by-token/{instrument_token}",
            "/market-data/candles/{instrument_token}",
            "/market-data/candles/{instrument_token}/latest",
            "/market-data/ticks/{instrument_token}/latest",
        }

    def test_health_reports_live_trading_not_implemented(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["live_trading_implemented"] is False
        assert body["trading_mode"] == "paper"


class TestNoOrderCapabilityInSource:
    """Static guarantee: no order operation exists anywhere in the application.

    Phase 1 is read-only market data. The application must remain incapable of
    placing, modifying or cancelling an order, and that is cheaper to assert
    over the source tree than to infer from behaviour.
    """

    FORBIDDEN_CALLABLES = (
        "def place_order",
        "def modify_order",
        "def cancel_order",
        "def exit_order",
        "def execute_trade",
        "place_order(",
        "modify_order(",
        "cancel_order(",
        ".orders(",
        "/orders",
    )

    @staticmethod
    def _sources() -> list[pathlib.Path]:
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        return sorted(root.rglob("*.py"))

    def test_no_order_operations_anywhere_in_app(self) -> None:
        offenders: list[str] = []
        for path in self._sources():
            text = path.read_text(encoding="utf-8")
            for needle in self.FORBIDDEN_CALLABLES:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"order capability must not exist: {offenders}"

    def test_no_write_handlers_exist_in_the_http_layer(self) -> None:
        """No POST/PUT/PATCH/DELETE handler - so no webhook and no order route."""
        api = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
        offenders: list[str] = []
        for path in sorted(api.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for verb in (".post(", ".put(", ".patch(", ".delete("):
                if f"@router{verb}" in text or f"@api_router{verb}" in text:
                    offenders.append(f"{path.name}: {verb}")
        assert offenders == []

    def test_no_llm_client_is_wired_in(self) -> None:
        offenders: list[str] = []
        for path in self._sources():
            text = path.read_text(encoding="utf-8").lower()
            if "import anthropic" in text or "from anthropic" in text:
                offenders.append(path.name)
        assert offenders == []

    def test_market_data_domain_does_not_import_a_broker(self) -> None:
        """The domain depends on ports, never on a vendor adapter."""
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "domain"
        offenders = [
            path.name
            for path in sorted(root.rglob("*.py"))
            if "app.adapters" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestDomainPurity:
    """The strategy and backtest domain must stay pure and deterministic.

    Phase 2 adds two packages under ``app/domain``. They exist there precisely so
    these scans apply to them: a strategy that could reach a broker, an ORM or a
    clock would be neither independently testable nor reproducible, and the
    reproducibility guarantee - same input, same result - would become a matter
    of reviewer vigilance rather than a property of the code.
    """

    #: Packages that must be pure: no vendor, no persistence, no I/O, no clock.
    PURE_PACKAGES = ("strategy", "backtest")

    @staticmethod
    def _domain_sources() -> list[pathlib.Path]:
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "domain"
        return sorted(root.rglob("*.py"))

    @classmethod
    def _pure_sources(cls) -> list[pathlib.Path]:
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "domain"
        paths: list[pathlib.Path] = []
        for package in cls.PURE_PACKAGES:
            paths.extend(sorted((root / package).rglob("*.py")))
        assert paths, "the pure domain packages must exist"
        return paths

    def test_the_pure_packages_are_present(self) -> None:
        names = {path.parent.name for path in self._pure_sources()}
        assert set(self.PURE_PACKAGES) <= names

    def test_domain_never_imports_an_adapter_or_a_broker(self) -> None:
        forbidden = ("app.adapters", "kiteconnect", "zerodha", "websockets", "httpx")
        offenders: list[str] = []
        for path in self._domain_sources():
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if f"import {needle}" in text or f"from {needle}" in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"the domain must not reach a vendor: {offenders}"

    def test_domain_never_imports_persistence(self) -> None:
        """Persistence is the infrastructure layer's job, behind a port."""
        offenders: list[str] = []
        for path in self._domain_sources():
            text = path.read_text(encoding="utf-8")
            for needle in ("sqlalchemy", "alembic", "psycopg", "app.infrastructure"):
                if f"import {needle}" in text or f"from {needle}" in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"the domain must not import persistence: {offenders}"

    def test_strategy_and_backtest_never_import_orders_execution_or_ai(self) -> None:
        """Architecture rule: strategy and exits never import broker, orders or ai."""
        forbidden = (
            "app.domain.orders",
            "app.domain.execution",
            "app.domain.broker",
            "app.domain.ai",
            "anthropic",
            "openai",
        )
        offenders: list[str] = []
        for path in self._pure_sources():
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text and "must not" not in text.split(needle)[0][-120:]:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"pure domain code must not reach execution or AI: {offenders}"

    def test_strategy_and_backtest_read_no_clock(self) -> None:
        """A run that reads a clock is not reproducible."""
        forbidden = ("datetime.now(", "utc_now(", "SystemClock", "time.time(", "monotonic(")
        offenders: list[str] = []
        for path in self._pure_sources():
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"pure domain code must not read a clock: {offenders}"

    def test_strategy_and_backtest_use_no_randomness(self) -> None:
        """Randomness arrives only in Phase 2.9, seeded, in its own module."""
        forbidden = ("import random", "from random", "import secrets", "numpy.random", "uuid4")
        offenders: list[str] = []
        for path in self._pure_sources():
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"pure domain code must be deterministic: {offenders}"

    def test_strategy_and_backtest_perform_no_io_and_read_no_environment(self) -> None:
        forbidden = ("open(", "pathlib", "os.environ", "os.getenv", "requests", "app.config")
        offenders: list[str] = []
        for path in self._pure_sources():
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], f"pure domain code must not touch the outside world: {offenders}"

    def test_reproducibility_fingerprints_never_use_builtin_hash(self) -> None:
        """``hash()`` is salted per process, so it cannot identify a run.

        Parsed rather than grepped: prose in a docstring explaining that hash()
        is avoided must not be mistaken for a call to it.
        """
        offenders: list[str] = []
        for path in self._pure_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "hash"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"use hashlib over canonical bytes, not hash(): {offenders}"

    def test_no_float_is_used_in_the_money_path(self) -> None:
        """Money is exact decimal. A float literal or cast here is a defect.

        Parsed rather than grepped, so the ``must be Decimal, never float``
        guards - which are what enforce this at runtime - are not themselves
        reported as violations.
        """
        offenders: list[str] = []
        for path in self._pure_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    offenders.append(f"{path.name}:{node.lineno} float literal")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "float"
                ):
                    offenders.append(f"{path.name}:{node.lineno} float() call")
        assert offenders == [], f"the money path must not use float: {offenders}"

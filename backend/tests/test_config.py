"""Configuration loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import (
    AppEnv,
    ConfigurationError,
    LogFormat,
    Settings,
    TradingMode,
)
from tests.conftest import build_settings


class TestLoading:
    def test_defaults_are_safe(self) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.app_env is AppEnv.DEVELOPMENT
        assert s.trading_mode is TradingMode.PAPER
        assert s.live_trading_armed is False
        assert s.live_capital_ceiling_inr == 0

    def test_values_are_typed_not_strings(self) -> None:
        s = build_settings(app_env="production", trading_mode="backtest")
        assert isinstance(s.app_env, AppEnv)
        assert isinstance(s.trading_mode, TradingMode)
        assert s.app_env is AppEnv.PRODUCTION
        assert s.trading_mode is TradingMode.BACKTEST

    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("TRADING_MODE", "backtest")
        monkeypatch.setenv("LOG_LEVEL", "debug")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.app_env is AppEnv.TEST
        assert s.trading_mode is TradingMode.BACKTEST
        assert s.log_level == "DEBUG"  # normalised by the validator

    def test_log_format_enum(self) -> None:
        assert build_settings(log_format="console").log_format is LogFormat.CONSOLE

    def test_cors_origins_parsed_to_list(self) -> None:
        s = build_settings(api_cors_origins="http://a.test, http://b.test ,")
        assert s.cors_origin_list == ["http://a.test", "http://b.test"]

    def test_sqlite_detection(self) -> None:
        assert build_settings(database_url="sqlite:///x.db").is_sqlite is True
        assert build_settings(database_url="postgresql+psycopg://u:p@h:5432/d").is_sqlite is False


class TestInvalidConfiguration:
    def test_unknown_trading_mode_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            build_settings(trading_mode="yolo")
        assert "trading_mode" in str(exc.value)

    def test_unknown_app_env_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            build_settings(app_env="staging")
        assert "app_env" in str(exc.value)

    def test_bad_log_level_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
            build_settings(log_level="chatty")

    def test_malformed_database_url_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="DATABASE_URL"):
            build_settings(database_url="just-a-string")

    def test_non_integer_port_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_settings(api_port="eight thousand")


class TestSecretHandling:
    def test_secrets_are_not_plain_strings(self) -> None:
        s = build_settings(zerodha_api_key="super-secret-key")
        assert s.zerodha_api_key is not None
        assert "super-secret-key" not in repr(s.zerodha_api_key)
        assert s.zerodha_api_key.get_secret_value() == "super-secret-key"

    def test_safe_dump_contains_no_secret_values(self) -> None:
        s = build_settings(
            zerodha_api_key="super-secret-key",
            zerodha_api_secret="super-secret-secret",
            zerodha_access_token="super-secret-token",
            anthropic_api_key="sk-should-never-appear",
            tradingview_webhook_secret="tv-hook-secret",
        )
        dumped = repr(s.safe_dump())
        for leaked in (
            "super-secret-key",
            "super-secret-secret",
            "super-secret-token",
            "sk-should-never-appear",
            "tv-hook-secret",
        ):
            assert leaked not in dumped

    def test_safe_dump_reports_presence_only(self) -> None:
        without = build_settings().safe_dump()
        with_creds = build_settings(zerodha_api_key="k").safe_dump()
        assert without["broker_credentials_present"] is False
        assert with_creds["broker_credentials_present"] is True

    def test_safe_dump_exposes_operating_context(self) -> None:
        dumped = build_settings(database_url="sqlite:///x.db").safe_dump()
        assert dumped["trading_mode"] == "paper"
        assert dumped["database_backend"] == "sqlite"
        assert dumped["live_trading_implemented"] is False

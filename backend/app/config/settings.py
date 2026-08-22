"""Strongly typed application configuration.

Two independent axes, deliberately not conflated:

*   ``APP_ENV``       — where the process is running (development / test / production)
*   ``TRADING_MODE``  — what it is allowed to do with money (backtest / paper / live)

Architecture v0.3 §19.1: the system defaults to PAPER and LIVE must never be
reachable by accident.  Three separate barriers enforce that here.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Phase gate.
#
# Live trading is NOT implemented.  There is no order-placement code path, no
# broker write adapter and no execution engine in this build.  This single flag
# is the last barrier; it is flipped only when the live phase is actually
# implemented and approved.  Do not flip it to make a test pass.
# ---------------------------------------------------------------------------
LIVE_TRADING_IMPLEMENTED = False

_REPO_ROOT = Path(__file__).resolve().parents[3]


class ConfigurationError(RuntimeError):
    """Raised when configuration is invalid or unsafe to run with."""


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application -------------------------------------------------------
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_name: str = "aitrade-backend"
    app_version: str = "0.0.1"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Trading mode ------------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_armed: bool = False
    live_capital_ceiling_inr: int = 0

    # -- Database ----------------------------------------------------------
    database_url: str = "postgresql+psycopg://aitrade:aitrade@localhost:5432/aitrade"
    db_pool_size: int = 5
    db_pool_max_overflow: int = 5
    db_echo: bool = False

    # -- API ---------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside the container/dev host
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:3000"

    # -- Broker (declared, NOT used in this phase) -------------------------
    zerodha_api_key: SecretStr | None = None
    zerodha_api_secret: SecretStr | None = None
    zerodha_access_token: SecretStr | None = None

    # -- Compliance profile (declared, NOT used in this phase) -------------
    compliance_algo_id: str | None = None
    compliance_classification: str | None = None
    compliance_registered_ip: str | None = None
    compliance_confirmation_ref: str | None = None

    # -- Not implemented in this phase -------------------------------------
    tradingview_webhook_secret: SecretStr | None = Field(default=None)
    anthropic_api_key: SecretStr | None = Field(default=None)

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def safe_dump(self) -> dict[str, object]:
        """Configuration rendered for logs and the health endpoint.

        Secrets are never included — not even masked values that could leak
        length information.  Only presence is reported.
        """
        return {
            "app_env": self.app_env.value,
            "app_name": self.app_name,
            "app_version": self.app_version,
            "trading_mode": self.trading_mode.value,
            "log_level": self.log_level,
            "log_format": self.log_format.value,
            "database_backend": self.database_url.split("://", 1)[0],
            "live_trading_implemented": LIVE_TRADING_IMPLEMENTED,
            "broker_credentials_present": self.zerodha_api_key is not None,
            "compliance_profile_present": self.compliance_algo_id is not None,
        }

    # ------------------------------------------------------------------ #
    # Safety validation
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _guard_live_mode(self) -> Settings:
        if self.trading_mode is not TradingMode.LIVE:
            return self

        # Barrier 1 — required configuration must be complete.
        missing = [
            name
            for name, value in (
                ("ZERODHA_API_KEY", self.zerodha_api_key),
                ("ZERODHA_API_SECRET", self.zerodha_api_secret),
                ("ZERODHA_ACCESS_TOKEN", self.zerodha_access_token),
                ("COMPLIANCE_ALGO_ID", self.compliance_algo_id),
                ("COMPLIANCE_CLASSIFICATION", self.compliance_classification),
                ("COMPLIANCE_REGISTERED_IP", self.compliance_registered_ip),
                ("COMPLIANCE_CONFIRMATION_REF", self.compliance_confirmation_ref),
            )
            if value is None
        ]
        if not self.live_trading_armed:
            missing.append("LIVE_TRADING_ARMED")
        if self.live_capital_ceiling_inr <= 0:
            missing.append("LIVE_CAPITAL_CEILING_INR")
        if missing:
            raise ConfigurationError(
                "LIVE trading mode requires configuration that is absent: "
                + ", ".join(sorted(missing))
            )

        # Barrier 2 — the phase gate. Reached only when everything above is set.
        if not LIVE_TRADING_IMPLEMENTED:
            raise ConfigurationError(
                "LIVE trading mode is not implemented in this build. "
                "No order-placement, broker write, or execution code path exists. "
                "Set TRADING_MODE=paper."
            )

        return self

    @model_validator(mode="after")
    def _validate_log_level(self) -> Settings:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if self.log_level.upper() not in allowed:
            raise ConfigurationError(
                f"LOG_LEVEL must be one of {sorted(allowed)}, got {self.log_level!r}"
            )
        self.log_level = self.log_level.upper()
        return self

    @model_validator(mode="after")
    def _validate_database_url(self) -> Settings:
        if "://" not in self.database_url:
            raise ConfigurationError(
                f"DATABASE_URL must be a SQLAlchemy URL, got {self.database_url!r}"
            )
        return self


_override: Settings | None = None


@functools.lru_cache
def _load_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Process-wide settings accessor.

    Configuration is read from the environment once and cached.  Tests install
    isolated settings with :func:`set_settings_override` rather than mutating
    the environment, so a developer's local ``.env`` can never change a test
    outcome.
    """
    return _override if _override is not None else _load_settings()


def set_settings_override(settings: Settings | None) -> None:
    """Install (or with ``None``, remove) an explicit settings instance."""
    global _override
    _override = settings


def clear_settings_cache() -> None:
    _load_settings.cache_clear()

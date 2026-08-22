"""Shared test fixtures.

The suite runs against a real database with real Alembic migrations applied.

``TEST_DATABASE_URL`` selects the backend:
  * unset  -> a throwaway SQLite file (local runs without Docker)
  * set    -> whatever it points at; CI points it at PostgreSQL

Nothing here reads the developer's ``.env`` — settings are always constructed
with ``_env_file=None`` so a local configuration cannot alter test outcomes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config.settings import (
    AppEnv,
    Settings,
    TradingMode,
    clear_settings_cache,
    set_settings_override,
)
from app.infrastructure import db as db_module

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def build_settings(**overrides: object) -> Settings:
    """Construct isolated settings, ignoring any local .env file."""
    values: dict[str, object] = {
        "app_env": AppEnv.TEST,
        "trading_mode": TradingMode.PAPER,
        "log_format": "console",
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def run_migrations(database_url: str) -> None:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    path = tmp_path_factory.mktemp("db") / "test.sqlite3"
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    """A database with the full migration history applied."""
    run_migrations(database_url)
    return database_url


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return build_settings(database_url=migrated_database_url)


@pytest.fixture
def wired_settings(settings: Settings) -> Iterator[Settings]:
    """Install ``settings`` as the process-wide settings and reset the engine."""
    db_module.reset_engine()
    set_settings_override(settings)
    try:
        yield settings
    finally:
        set_settings_override(None)
        clear_settings_cache()
        db_module.reset_engine()


@pytest.fixture
def app(wired_settings: Settings) -> FastAPI:
    from app.main import create_app

    return create_app(wired_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client

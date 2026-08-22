"""Database connectivity, migrations and the system_event table."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, select

from app.config.settings import Settings
from app.infrastructure import db as db_module
from app.infrastructure.db import check_database, get_engine, session_scope
from app.infrastructure.models import SystemEvent
from app.services.system_events import record_system_event
from tests.conftest import build_settings


class TestConnectivity:
    def test_probe_succeeds_against_a_real_database(self, wired_settings: Settings) -> None:
        result = check_database(wired_settings)
        assert result.ok is True
        assert result.error is None
        assert result.latency_ms is not None and result.latency_ms >= 0

    def test_probe_reports_failure_for_an_unreachable_database(self, tmp_path: Path) -> None:
        unreachable = tmp_path / "does-not-exist" / "nope.sqlite3"
        settings = build_settings(database_url=f"sqlite:///{unreachable.as_posix()}")
        db_module.reset_engine()
        try:
            result = check_database(settings)
        finally:
            db_module.reset_engine()
        assert result.ok is False
        assert result.error is not None

    def test_engine_is_reused(self, wired_settings: Settings) -> None:
        assert get_engine(wired_settings) is get_engine(wired_settings)


class TestMigrations:
    def test_migration_created_the_table(self, wired_settings: Settings) -> None:
        inspector = inspect(get_engine(wired_settings))
        assert "system_event" in inspector.get_table_names()

    def test_expected_columns_exist(self, wired_settings: Settings) -> None:
        columns = {
            c["name"] for c in inspect(get_engine(wired_settings)).get_columns("system_event")
        }
        assert columns == {
            "id",
            "occurred_at",
            "event_type",
            "app_env",
            "trading_mode",
            "app_version",
            "correlation_id",
            "detail",
        }

    def test_alembic_revision_is_recorded(self, wired_settings: Settings) -> None:
        result = check_database(wired_settings)
        assert result.migration_revision == "0001_system_event"
        assert result.details["migrated"] is True


class TestSystemEvents:
    def test_event_round_trips(self, wired_settings: Settings) -> None:
        assert record_system_event("test_event", wired_settings, {"k": "v"}) is True
        with session_scope() as session:
            row = session.execute(
                select(SystemEvent).where(SystemEvent.event_type == "test_event")
            ).scalar_one()
            assert row.trading_mode == "paper"
            assert row.app_env == "test"
            assert row.detail == {"k": "v"}
            assert row.occurred_at is not None

    def test_recording_never_raises_when_the_database_is_down(self, tmp_path: Path) -> None:
        unreachable = tmp_path / "gone" / "nope.sqlite3"
        settings = build_settings(database_url=f"sqlite:///{unreachable.as_posix()}")
        db_module.reset_engine()
        try:
            assert record_system_event("startup_without_db", settings) is False
        finally:
            db_module.reset_engine()

"""Health endpoints — they must probe real subsystems, not return constants."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.infrastructure import db as db_module
from tests.conftest import build_settings


class TestLiveness:
    def test_health_returns_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["environment"] == "test"
        assert body["trading_mode"] == "paper"
        assert body["live_trading_implemented"] is False
        assert body["uptime_seconds"] >= 0

    def test_health_does_not_leak_configuration(self, client: TestClient) -> None:
        text = client.get("/health").text.lower()
        for forbidden in ("secret", "token", "password", "database_url", "api_key"):
            assert forbidden not in text

    def test_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "abc123"})
        assert response.headers["X-Request-ID"] == "abc123"

    def test_request_id_is_generated_when_absent(self, client: TestClient) -> None:
        response = client.get("/health")
        assert len(response.headers.get("X-Request-ID", "")) >= 16


class TestDatabaseHealth:
    def test_reports_ok_and_the_applied_revision(self, client: TestClient) -> None:
        response = client.get("/health/db")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["migrated"] is True
        assert body["migration_revision"] == "0001_system_event"
        assert body["latency_ms"] is not None

    def test_returns_503_when_the_database_is_unreachable(
        self, tmp_path: Path, wired_settings: Settings
    ) -> None:
        from app.config.settings import set_settings_override
        from app.main import create_app

        broken = build_settings(
            database_url=f"sqlite:///{(tmp_path / 'gone' / 'x.sqlite3').as_posix()}"
        )
        db_module.reset_engine()
        set_settings_override(broken)
        try:
            with TestClient(create_app(broken)) as broken_client:
                response = broken_client.get("/health/db")
            assert response.status_code == 503
            body = response.json()
            assert body["status"] == "error"
            assert body["error"]
        finally:
            set_settings_override(wired_settings)
            db_module.reset_engine()

    def test_the_two_endpoints_are_independent(
        self, tmp_path: Path, wired_settings: Settings
    ) -> None:
        """A dead database must not take the liveness endpoint down with it."""
        from app.config.settings import set_settings_override
        from app.main import create_app

        broken = build_settings(
            database_url=f"sqlite:///{(tmp_path / 'gone2' / 'x.sqlite3').as_posix()}"
        )
        db_module.reset_engine()
        set_settings_override(broken)
        try:
            with TestClient(create_app(broken)) as broken_client:
                assert broken_client.get("/health").status_code == 200
                assert broken_client.get("/health/db").status_code == 503
        finally:
            set_settings_override(wired_settings)
            db_module.reset_engine()


class TestErrorHandling:
    def test_unknown_route_returns_problem_json(self, client: TestClient) -> None:
        response = client.get("/no-such-route")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["code"] == "http_error"
        assert body["correlation_id"]

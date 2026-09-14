"""Kite Connect login and callback, against a mocked Zerodha. The live API is never called."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.zerodha.client import ZerodhaRestClient
from app.api.zerodha_auth import zerodha_client
from app.config.settings import Settings, get_settings
from app.runtime.market_data import build_rest_client
from tests.conftest import build_settings

API_KEY = "kitekey123"
API_SECRET = "SECRET_do_not_leak_987"
REQUEST_TOKEN = "RqTok3nABCdef456"
ACCESS_TOKEN = "ACCESS_do_not_leak_654"
SECRETS = (API_SECRET, REQUEST_TOKEN, ACCESS_TOKEN)

Handler = Callable[[httpx.Request], httpx.Response]


def success(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "user_id": "AB1234",
                "user_name": "Test User",
                "access_token": ACCESS_TOKEN,
                "login_time": "2026-09-14 09:00:00",
            },
        },
    )


def broker_error(status: int, error_type: str, message: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"status": "error", "error_type": error_type, "message": message}
        )

    return handler


class Broker:
    """A mocked Zerodha behind the real ZerodhaRestClient."""

    def __init__(self) -> None:
        self.handler: Handler = success
        self.requests: list[httpx.Request] = []

    def client(self) -> ZerodhaRestClient:
        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        return ZerodhaRestClient(
            api_key=API_KEY,
            api_secret=API_SECRET,
            client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
        )


@pytest.fixture
def broker(app: FastAPI) -> Iterator[Broker]:
    fake = Broker()
    app.dependency_overrides[zerodha_client] = fake.client
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def settings_with_keys(wired_settings: Settings) -> Settings:
    """The process settings, holding the key and secret but no access token."""
    settings = get_settings()
    settings.zerodha_api_key = build_settings(zerodha_api_key=API_KEY).zerodha_api_key
    settings.zerodha_api_secret = build_settings(zerodha_api_secret=API_SECRET).zerodha_api_secret
    return settings


def callback(client: TestClient, **params: str) -> httpx.Response:
    return client.get("/auth/zerodha/callback", params=params)


class TestLogin:
    def test_redirects_to_the_clients_login_url(self, client: TestClient, broker: Broker) -> None:
        response = client.get("/auth/zerodha/login", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == broker.client().login_url()
        assert broker.requests == []

    def test_without_an_api_key_it_reports_not_configured(self, client: TestClient) -> None:
        response = client.get("/auth/zerodha/login", follow_redirects=False)
        assert response.status_code == 503
        assert response.json()["code"] == "zerodha_not_configured"


class TestCallbackSuccess:
    def test_exchanges_the_token_through_generate_session(
        self, client: TestClient, broker: Broker
    ) -> None:
        response = callback(client, request_token=REQUEST_TOKEN, action="login", status="success")

        assert response.status_code == 200
        assert response.json() == {
            "status": "authenticated",
            "user_id": "AB1234",
            "stored_for": "this_process",
            "note": (
                "Access token set for this API process only; it is not persisted. Other "
                "processes still read ZERODHA_ACCESS_TOKEN."
            ),
        }
        (sent,) = broker.requests
        assert (sent.method, sent.url.path) == ("POST", "/session/token")
        form = parse_qs(sent.content.decode())
        assert form["request_token"] == [REQUEST_TOKEN]
        expected = hashlib.sha256(f"{API_KEY}{REQUEST_TOKEN}{API_SECRET}".encode()).hexdigest()
        assert form["checksum"] == [expected]

    def test_the_token_is_set_for_this_process_only(
        self, client: TestClient, broker: Broker
    ) -> None:
        assert get_settings().zerodha_access_token is None
        callback(client, request_token=REQUEST_TOKEN)
        stored = get_settings().zerodha_access_token
        assert stored is not None and stored.get_secret_value() == ACCESS_TOKEN
        assert get_settings().zerodha_secret("access_token") == ACCESS_TOKEN

    def test_the_real_dependency_uses_configured_keys(
        self, client: TestClient, settings_with_keys: Settings
    ) -> None:
        response = client.get("/auth/zerodha/login", follow_redirects=False)
        assert response.headers["location"] == (
            f"https://kite.zerodha.com/connect/login?v=3&api_key={API_KEY}"
        )


class TestCallbackRefusals:
    @pytest.mark.parametrize("params", [{}, {"request_token": ""}, {"request_token": "   "}])
    def test_a_missing_or_empty_token_is_refused_without_calling_zerodha(
        self, client: TestClient, broker: Broker, params: dict[str, str]
    ) -> None:
        response = callback(client, **params)
        assert response.status_code == 422
        assert response.json()["detail"] == "request_token is required"
        assert broker.requests == []

    def test_a_malformed_token_is_refused_and_not_echoed(
        self, client: TestClient, broker: Broker
    ) -> None:
        response = callback(client, request_token="abc$<script>")
        assert response.status_code == 422
        assert response.json()["detail"] == "request_token is malformed"
        assert "abc$" not in response.text
        assert broker.requests == []

    def test_a_failed_login_status_is_refused(self, client: TestClient, broker: Broker) -> None:
        response = callback(client, request_token=REQUEST_TOKEN, status="error")
        assert response.status_code == 401
        assert broker.requests == []

    @pytest.mark.parametrize(
        "handler",
        [
            broker_error(403, "TokenException", "Token is invalid or has expired."),
            broker_error(400, "InputException", "Invalid `request_token`."),
        ],
    )
    def test_an_invalid_token_is_an_authentication_failure(
        self, client: TestClient, broker: Broker, handler: Handler
    ) -> None:
        broker.handler = handler
        response = callback(client, request_token=REQUEST_TOKEN)
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "zerodha_auth_failed"
        assert "/auth/zerodha/login" in body["detail"]
        assert "error_type" not in body
        assert get_settings().zerodha_access_token is None

    def test_a_network_failure(self, client: TestClient, broker: Broker) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        broker.handler = unreachable
        response = callback(client, request_token=REQUEST_TOKEN)
        assert response.status_code == 503
        assert response.json()["code"] == "zerodha_network_error"
        assert get_settings().zerodha_access_token is None

    def test_a_response_without_an_access_token(self, client: TestClient, broker: Broker) -> None:
        broker.handler = lambda request: httpx.Response(200, json={"data": {"user_id": "AB1234"}})
        response = callback(client, request_token=REQUEST_TOKEN)
        assert response.status_code == 502
        assert response.json()["code"] == "zerodha_protocol_error"
        assert get_settings().zerodha_access_token is None

    def test_an_unexpected_client_failure_leaks_nothing(
        self, app: FastAPI, client: TestClient
    ) -> None:
        class Exploding(ZerodhaRestClient):
            async def generate_session(self, request_token: str):  # type: ignore[no-untyped-def]
                raise RuntimeError(f"boom {API_SECRET} {request_token}")

        app.dependency_overrides[zerodha_client] = lambda: Exploding(
            api_key=API_KEY, api_secret=API_SECRET
        )
        try:
            response = callback(client, request_token=REQUEST_TOKEN)
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 502
        assert response.json()["detail"] == "token exchange failed unexpectedly"
        assert not any(secret in response.text for secret in SECRETS)

    def test_no_secret_configured_reports_not_configured(
        self, client: TestClient, wired_settings: Settings
    ) -> None:
        get_settings().zerodha_api_key = build_settings(zerodha_api_key=API_KEY).zerodha_api_key
        response = callback(client, request_token=REQUEST_TOKEN)
        assert response.status_code == 503
        assert response.json()["code"] == "zerodha_not_configured"


class TestNoLeaks:
    def test_no_response_or_log_carries_a_secret(
        self,
        client: TestClient,
        broker: Broker,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Alembic's fileConfig (run by the migration fixture) disables loggers that
        # already existed; re-enable the app's so this test sees every line it emits.
        for name, candidate in logging.root.manager.loggerDict.items():
            if name.startswith("app") and isinstance(candidate, logging.Logger):
                monkeypatch.setattr(candidate, "disabled", False)
        caplog.set_level(logging.DEBUG, logger="app")
        responses = [callback(client, request_token=REQUEST_TOKEN)]
        broker.handler = broker_error(403, "TokenException", "Token is invalid or has expired.")
        responses.append(callback(client, request_token=REQUEST_TOKEN))
        responses.append(client.get("/auth/zerodha/login", follow_redirects=False))

        assert [r.status_code for r in responses] == [200, 401, 307]
        for response in responses:
            exposed = response.text + json.dumps(dict(response.headers))
            assert not any(secret in exposed for secret in SECRETS)

        logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
        streams = capsys.readouterr()
        for secret in SECRETS:
            assert secret not in logged
            assert secret not in streams.out + streams.err
        # the flow did log - the session, redacted, and the refused exchange
        assert "zerodha_session_established" in logged
        assert "application_error" in logged


def test_the_manual_environment_token_flow_is_unchanged() -> None:
    settings = build_settings(
        zerodha_api_key=API_KEY, zerodha_api_secret=API_SECRET, zerodha_access_token="manual"
    )
    client = build_rest_client(settings)
    assert client.is_configured and client.access_token == "manual"

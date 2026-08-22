"""Zerodha REST client: configuration, auth, instrument parsing, safety."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.adapters.zerodha.client import (
    ZerodhaRestClient,
    parse_instruments_csv,
)
from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaInputError,
    ZerodhaNetworkError,
    ZerodhaNotConfiguredError,
    ZerodhaProtocolError,
    ZerodhaRateLimitedError,
    classify_response,
)

RETRIEVED_AT = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)

CSV_HEADER = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange"
)
CSV_BODY = "\n".join(
    [
        CSV_HEADER,
        "738561,2885,RELIANCE,RELIANCE INDUSTRIES,1400.5,,0,0.05,1,EQ,NSE,NSE",
        "256265,1001,NIFTY 50,NIFTY 50,25142.1,,0,0.05,0,EQ,INDICES,NSE",
        "12345678,48225,NIFTY26AUGFUT,NIFTY,25200,2026-08-27,0,0.05,75,FUT,NFO-FUT,NFO",
    ]
)


def client_with(handler, **kwargs) -> ZerodhaRestClient:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    return ZerodhaRestClient(
        client=httpx.AsyncClient(transport=transport),
        api_key=kwargs.pop("api_key", "test_key"),
        api_secret=kwargs.pop("api_secret", "test_secret"),
        access_token=kwargs.pop("access_token", "test_token"),
        **kwargs,
    )


class TestConfiguration:
    def test_unconfigured_client_reports_so(self) -> None:
        client = ZerodhaRestClient(api_key=None)
        assert client.is_configured is False
        assert client.can_exchange_token is False

    def test_key_without_token_is_not_configured(self) -> None:
        assert ZerodhaRestClient(api_key="k").is_configured is False

    def test_login_url_requires_an_api_key(self) -> None:
        with pytest.raises(ZerodhaNotConfiguredError, match="ZERODHA_API_KEY"):
            ZerodhaRestClient(api_key=None).login_url()

    def test_login_url_shape(self) -> None:
        url = ZerodhaRestClient(api_key="abc123").login_url()
        assert url == "https://kite.zerodha.com/connect/login?v=3&api_key=abc123"

    def test_websocket_url_requires_credentials(self) -> None:
        with pytest.raises(ZerodhaNotConfiguredError):
            ZerodhaRestClient(api_key="k").websocket_url()

    def test_websocket_url_carries_key_and_token(self) -> None:
        client = ZerodhaRestClient(api_key="k", access_token="t")
        assert client.websocket_url() == "wss://ws.kite.trade?api_key=k&access_token=t"

    def test_repr_never_leaks_the_token(self) -> None:
        client = ZerodhaRestClient(
            api_key="k", api_secret="super-secret", access_token="super-token"
        )
        text = repr(client)
        assert "super-secret" not in text
        assert "super-token" not in text


class TestChecksum:
    def test_matches_the_documented_formula(self) -> None:
        client = ZerodhaRestClient(api_key="key", api_secret="secret")
        expected = hashlib.sha256(b"keyreqsecret").hexdigest()
        assert client.checksum("req") == expected

    def test_requires_the_api_secret(self) -> None:
        with pytest.raises(ZerodhaNotConfiguredError, match="ZERODHA_API_SECRET"):
            ZerodhaRestClient(api_key="key").checksum("req")


class TestAuthentication:
    async def test_generate_session_stores_the_access_token(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "user_id": "AB1234",
                        "user_name": "Test User",
                        "access_token": "fresh_token",
                        "login_time": "2026-08-21 08:30:00",
                    },
                },
            )

        client = client_with(handler, access_token=None)
        session = await client.generate_session("request_token_value")
        assert session.access_token == "fresh_token"
        assert client.access_token == "fresh_token"
        assert "/session/token" in str(seen["url"])
        assert "checksum=" in str(seen["body"])

    async def test_session_redaction_hides_the_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": {"user_id": "AB1", "user_name": "N", "access_token": "zzz"}},
            )

        session = await client_with(handler, access_token=None).generate_session("r")
        assert "zzz" not in str(session.redacted())
        assert session.redacted()["access_token_present"] is True

    async def test_verify_session_succeeds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "token test_key:test_token"
            assert request.headers["X-Kite-Version"] == "3"
            return httpx.Response(200, json={"data": {"user_id": "AB1234"}})

        profile = await client_with(handler).verify_session()
        assert profile["user_id"] == "AB1234"

    async def test_expired_token_raises_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "status": "error",
                    "message": "Incorrect `api_key` or `access_token`.",
                    "error_type": "TokenException",
                },
            )

        with pytest.raises(ZerodhaAuthError, match="access_token"):
            await client_with(handler).verify_session()

    async def test_unconfigured_client_cannot_call_authenticated_endpoints(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made")

        client = client_with(handler, api_key=None, access_token=None)
        with pytest.raises(ZerodhaNotConfiguredError):
            await client.verify_session()

    async def test_transport_failure_becomes_a_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(ZerodhaNetworkError):
            await client_with(handler).verify_session()

    async def test_non_json_response_is_a_protocol_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        with pytest.raises(ZerodhaProtocolError):
            await client_with(handler).verify_session()


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "error_type", "expected"),
        [
            (403, "TokenException", ZerodhaAuthError),
            (400, "InputException", ZerodhaInputError),
            (429, None, ZerodhaRateLimitedError),
            (502, None, ZerodhaNetworkError),
            (500, "DataException", ZerodhaNetworkError),
        ],
    )
    def test_maps_status_and_type(self, status, error_type, expected) -> None:  # noqa: ANN001
        assert isinstance(classify_response(status, error_type, "msg"), expected)

    def test_context_is_preserved(self) -> None:
        error = classify_response(403, "TokenException", "expired")
        assert error.context["http_status"] == 403
        assert error.context["error_type"] == "TokenException"


class TestInstrumentParsing:
    def test_parses_every_documented_column(self) -> None:
        instruments = parse_instruments_csv(CSV_BODY, retrieved_at=RETRIEVED_AT)
        assert len(instruments) == 3
        reliance = next(i for i in instruments if i.tradingsymbol == "RELIANCE")
        assert reliance.instrument_token == 738561
        assert reliance.exchange == "NSE"
        assert reliance.segment == "NSE"
        assert reliance.instrument_type == "EQ"
        assert reliance.tick_size == Decimal("0.05")
        assert reliance.lot_size == 1
        assert reliance.last_price == Decimal("1400.5")
        assert reliance.expiry is None
        assert reliance.retrieved_at == RETRIEVED_AT

    def test_derivative_expiry_and_lot_size(self) -> None:
        instruments = parse_instruments_csv(CSV_BODY, retrieved_at=RETRIEVED_AT)
        future = next(i for i in instruments if i.instrument_type == "FUT")
        assert future.expiry is not None
        assert future.expiry.isoformat() == "2026-08-27"
        assert future.lot_size == 75
        assert future.exchange == "NFO"

    def test_index_rows_are_marked_not_tradable(self) -> None:
        instruments = parse_instruments_csv(CSV_BODY, retrieved_at=RETRIEVED_AT)
        nifty = next(i for i in instruments if i.tradingsymbol == "NIFTY 50")
        assert nifty.is_index is True
        assert nifty.is_tradable is False

    def test_stable_key_is_exchange_plus_symbol(self) -> None:
        instruments = parse_instruments_csv(CSV_BODY, retrieved_at=RETRIEVED_AT)
        assert instruments[0].key == "NSE:RELIANCE"

    def test_malformed_rows_are_skipped_not_fatal(self) -> None:
        text = CSV_BODY + "\nnot-a-token,,,,,,,,,,,\n,,MISSINGTOKEN,,,,,,,,,"
        instruments = parse_instruments_csv(text, retrieved_at=RETRIEVED_AT)
        assert len(instruments) == 3

    def test_missing_columns_are_rejected(self) -> None:
        with pytest.raises(ZerodhaProtocolError, match="missing expected columns"):
            parse_instruments_csv("instrument_token,tradingsymbol\n1,X", retrieved_at=RETRIEVED_AT)

    def test_empty_dump_is_rejected(self) -> None:
        with pytest.raises(ZerodhaProtocolError):
            parse_instruments_csv("", retrieved_at=RETRIEVED_AT)

    async def test_fetch_instruments_end_to_end(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/instruments"
            return httpx.Response(200, text=CSV_BODY)

        instruments = await client_with(handler).fetch_instruments()
        assert len(instruments) == 3
        assert all(i.source == "zerodha" for i in instruments)

    async def test_fetch_instruments_for_one_exchange(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/instruments/NSE"
            return httpx.Response(200, text=CSV_BODY)

        assert await client_with(handler).fetch_instruments("NSE")


class TestReadOnlySafety:
    """The adapter must have no order capability whatsoever."""

    def test_client_exposes_no_order_methods(self) -> None:
        forbidden = {
            "place_order",
            "modify_order",
            "cancel_order",
            "orders",
            "positions",
            "margins",
            "exit_order",
        }
        assert forbidden.isdisjoint(dir(ZerodhaRestClient))

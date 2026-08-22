"""Zerodha Kite Connect REST client - read-only.

Implements only what market data needs:

*   the login URL and the ``request_token`` -> ``access_token`` exchange,
*   a profile call used purely to verify that a token is live,
*   the daily instruments dump.

**There are no order methods here and none may be added in this phase.** No
``place_order``, ``modify_order`` or ``cancel_order`` exists anywhere in this
package, and a test asserts that.

Reference: https://kite.trade/docs/connect/v3/
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaNetworkError,
    ZerodhaNotConfiguredError,
    ZerodhaProtocolError,
    classify_response,
)
from app.core.logging import get_logger
from app.core.time import utc_now
from app.domain.market.models import Instrument

logger = get_logger(__name__)

__all__ = ["ZerodhaRestClient", "ZerodhaSession", "parse_instruments_csv"]

API_ROOT = "https://api.kite.trade"
LOGIN_ROOT = "https://kite.zerodha.com/connect/login"
KITE_VERSION = "3"

_INSTRUMENT_COLUMNS = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)


@dataclass(frozen=True, slots=True)
class ZerodhaSession:
    """Result of a successful token exchange.

    Deliberately does not carry the secret: callers store the access token in
    configuration, and it is never logged or persisted by this package.
    """

    user_id: str
    user_name: str
    access_token: str
    login_time: str | None = None

    def redacted(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "login_time": self.login_time,
            "access_token_present": bool(self.access_token),
        }


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _expiry(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_instruments_csv(
    text: str, *, retrieved_at: datetime, source: str = "zerodha"
) -> list[Instrument]:
    """Parse the instruments dump.

    Rows that cannot be parsed are skipped and counted rather than aborting the
    whole refresh: one malformed row must not cost the entire instrument master.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ZerodhaProtocolError("instruments dump was empty")

    missing = [c for c in _INSTRUMENT_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ZerodhaProtocolError(
            "instruments dump is missing expected columns",
            missing_columns=missing,
        )

    instruments: list[Instrument] = []
    skipped = 0
    for row in reader:
        try:
            token = int(row["instrument_token"])
            exchange_token = int(row["exchange_token"] or 0)
            lot_size = int(row["lot_size"] or 0)
        except (TypeError, ValueError):
            skipped += 1
            continue

        tradingsymbol = (row["tradingsymbol"] or "").strip()
        exchange = (row["exchange"] or "").strip()
        if not tradingsymbol or not exchange:
            skipped += 1
            continue

        instruments.append(
            Instrument(
                instrument_token=token,
                exchange_token=exchange_token,
                tradingsymbol=tradingsymbol,
                name=(row["name"] or "").strip(),
                exchange=exchange,
                segment=(row["segment"] or "").strip(),
                instrument_type=(row["instrument_type"] or "").strip(),
                tick_size=_decimal(row["tick_size"]) or Decimal("0"),
                lot_size=lot_size,
                expiry=_expiry(row["expiry"]),
                strike=_decimal(row["strike"]),
                last_price=_decimal(row["last_price"]),
                source=source,
                retrieved_at=retrieved_at,
            )
        )

    if skipped:
        logger.warning(
            "instrument_rows_skipped", extra={"skipped": skipped, "parsed": len(instruments)}
        )
    return instruments


class ZerodhaRestClient:
    """Thin, read-only REST client.

    ``client`` may be injected so tests can drive it with a mock transport; the
    suite never touches the live API.
    """

    name = "zerodha"

    def __init__(
        self,
        *,
        api_key: str | None,
        api_secret: str | None = None,
        access_token: str | None = None,
        api_root: str = API_ROOT,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._access_token = access_token
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #

    @property
    def is_configured(self) -> bool:
        """Enough configuration to attempt an authenticated call."""
        return bool(self._api_key and self._access_token)

    @property
    def can_exchange_token(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def set_access_token(self, token: str | None) -> None:
        self._access_token = token

    def login_url(self) -> str:
        if not self._api_key:
            raise ZerodhaNotConfiguredError("ZERODHA_API_KEY is not set")
        return f"{LOGIN_ROOT}?v=3&api_key={self._api_key}"

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {"X-Kite-Version": KITE_VERSION}
        if authenticated:
            if not self.is_configured:
                raise ZerodhaNotConfiguredError(
                    "ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required"
                )
            headers["Authorization"] = f"token {self._api_key}:{self._access_token}"
        return headers

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        data: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        client = await self._http()
        url = f"{self._api_root}{path}"
        try:
            response = await client.request(
                method, url, headers=self._headers(authenticated=authenticated), data=data
            )
        except httpx.HTTPError as exc:
            raise ZerodhaNetworkError(
                f"{type(exc).__name__} contacting the broker", path=path
            ) from exc

        if response.status_code >= 400:
            error_type, message = self._error_envelope(response)
            raise classify_response(response.status_code, error_type, message)

        if not expect_json:
            return response.text

        try:
            body = response.json()
        except ValueError as exc:
            raise ZerodhaProtocolError("response was not valid JSON", path=path) from exc

        if not isinstance(body, dict) or "data" not in body:
            raise ZerodhaProtocolError("response envelope had no 'data'", path=path)
        return body["data"]

    @staticmethod
    def _error_envelope(response: httpx.Response) -> tuple[str | None, str]:
        try:
            body = response.json()
        except ValueError:
            return None, response.text[:200]
        if not isinstance(body, dict):
            return None, str(body)[:200]
        return body.get("error_type"), str(body.get("message", ""))[:500]

    # ------------------------------------------------------------------ #
    # session
    # ------------------------------------------------------------------ #

    def checksum(self, request_token: str) -> str:
        """SHA-256 of api_key + request_token + api_secret, per the spec."""
        if not self.can_exchange_token:
            raise ZerodhaNotConfiguredError(
                "ZERODHA_API_KEY and ZERODHA_API_SECRET are required to exchange a token"
            )
        raw = f"{self._api_key}{request_token}{self._api_secret}".encode()
        return hashlib.sha256(raw).hexdigest()

    async def generate_session(self, request_token: str) -> ZerodhaSession:
        """Exchange a one-time ``request_token`` for an access token."""
        data = await self._request(
            "POST",
            "/session/token",
            authenticated=False,
            data={
                "api_key": self._api_key or "",
                "request_token": request_token,
                "checksum": self.checksum(request_token),
            },
        )
        token = data.get("access_token")
        if not token:
            raise ZerodhaProtocolError("session response carried no access_token")
        self._access_token = token
        session = ZerodhaSession(
            user_id=str(data.get("user_id", "")),
            user_name=str(data.get("user_name", "")),
            access_token=token,
            login_time=data.get("login_time"),
        )
        logger.info("zerodha_session_established", extra=session.redacted())
        return session

    async def verify_session(self) -> dict[str, Any]:
        """Confirm the access token is live.

        Raises :class:`ZerodhaAuthError` when it is not, so callers cannot
        proceed as though a dead session were healthy.
        """
        data = await self._request("GET", "/user/profile")
        if not isinstance(data, dict):
            raise ZerodhaProtocolError("profile response was not an object")
        return data

    # ------------------------------------------------------------------ #
    # instruments
    # ------------------------------------------------------------------ #

    async def fetch_instruments(self, exchange: str | None = None) -> list[Instrument]:
        """Download and parse the instrument dump (CSV, generated once a day)."""
        path = "/instruments" if not exchange else f"/instruments/{exchange}"
        text = await self._request("GET", path, expect_json=False)
        retrieved_at = utc_now()
        instruments = parse_instruments_csv(text, retrieved_at=retrieved_at, source=self.name)
        logger.info(
            "instruments_fetched",
            extra={"count": len(instruments), "exchange": exchange or "ALL"},
        )
        return instruments

    # ------------------------------------------------------------------ #

    def websocket_url(self, root: str = "wss://ws.kite.trade") -> str:
        if not self.is_configured:
            raise ZerodhaNotConfiguredError(
                "ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required to stream"
            )
        return f"{root}?api_key={self._api_key}&access_token={self._access_token}"

    def __repr__(self) -> str:  # pragma: no cover - never leaks the secret
        return f"ZerodhaRestClient(configured={self.is_configured}, api_root={self._api_root!r})"


# Re-exported so callers can catch it without importing the errors module.
AuthError = ZerodhaAuthError

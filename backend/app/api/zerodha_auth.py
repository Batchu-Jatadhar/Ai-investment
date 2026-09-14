"""Kite Connect interactive login: redirect to Zerodha, receive the callback, mint a token.

Register ``http://127.0.0.1:8000/auth/zerodha/callback`` as the redirect URL.

Authentication only - these routes read no market data and place nothing. The
URL is built by :meth:`ZerodhaRestClient.login_url` and the exchange is
:meth:`ZerodhaRestClient.generate_session`; nothing here re-implements either.

.. rubric:: Where the token goes

There is no persistent credential store. The minted token is placed on this
process's settings (``zerodha_access_token``, the same field
``ZERODHA_ACCESS_TOKEN`` populates), so it lives only as long as this API
process. Other processes - the ``aitrade-marketdata`` streamer included - still
read ``ZERODHA_ACCESS_TOKEN`` from their environment, and that manual flow is
unchanged.

No response or log line carries the request token, the access token or the API
secret, and the request middleware logs the path without its query string.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, SecretStr

from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaError,
    ZerodhaInputError,
    ZerodhaNotConfiguredError,
)
from app.config.settings import get_settings
from app.core.errors import ValidationFailedError
from app.core.logging import get_logger

router = APIRouter(prefix="/auth/zerodha", tags=["auth"])
logger = get_logger(__name__)

_REQUEST_TOKEN = re.compile(r"[A-Za-z0-9]{1,128}")


async def zerodha_client() -> AsyncIterator[ZerodhaRestClient]:
    """The configured client, built exactly as the market-data runner builds it."""
    from app.runtime.market_data import build_rest_client

    client = build_rest_client(get_settings())
    try:
        yield client
    finally:
        await client.aclose()


class ZerodhaLoginResult(BaseModel):
    status: str
    user_id: str
    stored_for: str
    note: str


@router.get("/login", summary="Redirect to the Kite Connect login page")
def login(client: ZerodhaRestClient = Depends(zerodha_client)) -> RedirectResponse:
    return RedirectResponse(client.login_url(), status_code=307)


@router.get(
    "/callback",
    response_model=ZerodhaLoginResult,
    summary="Kite Connect redirect target: exchange request_token for an access token",
)
async def callback(
    request_token: str | None = Query(default=None),
    status: str | None = Query(default=None),
    client: ZerodhaRestClient = Depends(zerodha_client),
) -> ZerodhaLoginResult:
    if status is not None and status != "success":
        raise ZerodhaAuthError("Zerodha did not report a successful login")
    token = (request_token or "").strip()
    if not token:
        raise ValidationFailedError("request_token is required")
    if not _REQUEST_TOKEN.fullmatch(token):
        raise ValidationFailedError("request_token is malformed")

    try:
        session = await client.generate_session(token)
    except ZerodhaNotConfiguredError:
        raise
    except (ZerodhaAuthError, ZerodhaInputError):
        raise ZerodhaAuthError(
            "Zerodha rejected the request token: it is invalid, expired or already used. "
            "Start again at /auth/zerodha/login."
        ) from None
    except ZerodhaError as exc:
        raise type(exc)(f"token exchange failed: {exc.title}") from None
    except Exception as exc:  # an unknown failure must not leak its message
        logger.error("zerodha_token_exchange_failed", extra={"error_type": type(exc).__name__})
        raise ZerodhaError("token exchange failed unexpectedly") from None

    get_settings().zerodha_access_token = SecretStr(session.access_token)
    return ZerodhaLoginResult(
        status="authenticated",
        user_id=session.user_id,
        stored_for="this_process",
        note=(
            "Access token set for this API process only; it is not persisted. Other "
            "processes still read ZERODHA_ACCESS_TOKEN."
        ),
    )

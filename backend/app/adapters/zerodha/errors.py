"""Zerodha error taxonomy.

The broker returns a JSON envelope ``{"status", "message", "error_type"}``.
Translating it here keeps broker vocabulary inside the adapter: the domain sees
typed failures, never an ``error_type`` string.

The distinction that matters most is **authentication failure versus everything
else**. A 403 / ``TokenException`` means the session is gone and the only cure
is a human re-login; retrying it forever is pointless and hides the problem.
"""

from __future__ import annotations

from app.core.errors import AppError

__all__ = [
    "ZerodhaAuthError",
    "ZerodhaError",
    "ZerodhaInputError",
    "ZerodhaNetworkError",
    "ZerodhaNotConfiguredError",
    "ZerodhaProtocolError",
    "ZerodhaRateLimitedError",
    "classify_response",
]


class ZerodhaError(AppError):
    code = "zerodha_error"
    status_code = 502
    title = "Broker request failed"


class ZerodhaNotConfiguredError(ZerodhaError):
    """No API key / access token supplied.

    Not a failure of the broker - a failure to configure. The application still
    starts; it simply has no live market data.
    """

    code = "zerodha_not_configured"
    status_code = 503
    title = "Zerodha is not configured"


class ZerodhaAuthError(ZerodhaError):
    """Invalid or expired session. Requires a fresh interactive login.

    Kite access tokens expire at 06:00 IST the next day by regulation, so this
    is an expected daily event, not an exceptional one.
    """

    code = "zerodha_auth_failed"
    status_code = 401
    title = "Zerodha authentication failed"


class ZerodhaInputError(ZerodhaError):
    code = "zerodha_input_invalid"
    status_code = 400
    title = "Zerodha rejected the request"


class ZerodhaRateLimitedError(ZerodhaError):
    code = "zerodha_rate_limited"
    status_code = 429
    title = "Zerodha rate limit exceeded"


class ZerodhaNetworkError(ZerodhaError):
    """Transport failure, or the broker reporting an upstream problem."""

    code = "zerodha_network_error"
    status_code = 503
    title = "Zerodha is unreachable"


class ZerodhaProtocolError(ZerodhaError):
    """A response that did not match the documented shape."""

    code = "zerodha_protocol_error"
    status_code = 502
    title = "Unexpected broker response"


_AUTH_TYPES = frozenset({"TokenException"})
_INPUT_TYPES = frozenset({"InputException", "UserException"})
_NETWORK_TYPES = frozenset({"NetworkException", "DataException", "GeneralException"})


def classify_response(status_code: int, error_type: str | None, message: str) -> ZerodhaError:
    """Map an HTTP status and ``error_type`` onto a typed error.

    The message is passed through verbatim because the broker's wording is often
    the only clue; it never contains credentials.
    """
    detail = message or f"HTTP {status_code}"
    context = {"http_status": status_code, "error_type": error_type}

    if status_code == 403 or (error_type or "") in _AUTH_TYPES:
        return ZerodhaAuthError(detail, **context)
    if status_code == 429:
        return ZerodhaRateLimitedError(detail, **context)
    if status_code == 400 or (error_type or "") in _INPUT_TYPES:
        return ZerodhaInputError(detail, **context)
    if status_code >= 500 or (error_type or "") in _NETWORK_TYPES:
        return ZerodhaNetworkError(detail, **context)
    return ZerodhaError(detail, **context)

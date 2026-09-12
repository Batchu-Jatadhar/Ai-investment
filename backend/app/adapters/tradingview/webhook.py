"""TradingView alert webhook: the payload contract and its validation.

TradingView posts the alert's message text as the request body and cannot add
custom headers or sign the request. So the shared secret travels inside the
JSON body, and the body is treated as hostile until it has proved otherwise.

.. rubric:: The contract

Configure the TradingView alert message as exactly this JSON (``{{timenow}}``
is TradingView's placeholder for the fire time, in UTC)::

    {
      "secret": "<TRADINGVIEW_WEBHOOK_SECRET>",
      "alert_id": "orb-reliance-{{timenow}}",
      "symbol": "NSE:RELIANCE",
      "action": "long",
      "timestamp": "{{timenow}}",
      "note": "optional, up to 280 characters"
    }

Any other field is refused - there is deliberately no field for a quantity,
price, stop or target, and sending one is an error rather than something
quietly ignored.

.. rubric:: Order of checks

Every request meets the same sequence, so the same bad request always gets the
same answer:

1. body size (413) - checked by the endpoint before parsing
2. JSON object (422 ``webhook_malformed``)
3. secret, compared in constant time (401 ``webhook_unauthorized``) - before
   schema validation, so an unauthenticated caller learns nothing about the
   contract
4. strict schema (422 ``webhook_malformed``)
5. freshness: older than the maximum age, or too far in the future
   (422 ``webhook_stale``)

Duplicates (409) are decided afterwards by the ledger, atomically.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import AppError
from app.core.time import ensure_utc
from app.domain.alerts import AlertAction, ExternalAlert

__all__ = [
    "SOURCE",
    "TradingViewAlertPayload",
    "WebhookDuplicateError",
    "WebhookMalformedError",
    "WebhookModeNotPermittedError",
    "WebhookNotConfiguredError",
    "WebhookPayloadTooLargeError",
    "WebhookStaleError",
    "WebhookUnauthorizedError",
    "parse_alert",
]

SOURCE = "tradingview"


class WebhookNotConfiguredError(AppError):
    code = "webhook_not_configured"
    status_code = 503
    title = "Webhook is not configured"


class WebhookModeNotPermittedError(AppError):
    code = "webhook_mode_not_permitted"
    status_code = 403
    title = "Webhook is accepted in paper mode only"


class WebhookPayloadTooLargeError(AppError):
    code = "webhook_payload_too_large"
    status_code = 413
    title = "Webhook payload too large"


class WebhookMalformedError(AppError):
    code = "webhook_malformed"
    status_code = 422
    title = "Webhook payload is malformed"


class WebhookUnauthorizedError(AppError):
    code = "webhook_unauthorized"
    status_code = 401
    title = "Webhook authentication failed"


class WebhookStaleError(AppError):
    code = "webhook_stale"
    status_code = 422
    title = "Webhook alert is stale"


class WebhookDuplicateError(AppError):
    code = "webhook_duplicate"
    status_code = 409
    title = "Webhook alert already received"


class TradingViewAlertPayload(BaseModel):
    """The strict inbound contract. Unknown fields are errors."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    secret: str = Field(min_length=1, max_length=256)
    alert_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
    symbol: str = Field(pattern=r"^[A-Z]{2,10}:[A-Z0-9][A-Z0-9&_\-]{0,39}$")
    action: AlertAction
    timestamp: AwareDatetime
    note: str = Field(default="", max_length=280)


def _schema_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Field locations and messages only - never the submitted values."""
    return [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors(include_input=False, include_url=False)
    ]


def parse_alert(
    body: bytes,
    *,
    secret: str,
    now: datetime,
    max_age: timedelta,
    max_future: timedelta,
) -> ExternalAlert:
    """Authenticate, validate and normalise one webhook body. See the module docstring.

    ``now`` is supplied by the caller; nothing here reads a clock.
    """
    now = ensure_utc(now)
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookMalformedError("the body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise WebhookMalformedError("the body must be a JSON object")

    provided = document.get("secret")
    if not isinstance(provided, str) or not hmac.compare_digest(
        provided.encode("utf-8"), secret.encode("utf-8")
    ):
        raise WebhookUnauthorizedError("the webhook secret is missing or wrong")

    try:
        payload = TradingViewAlertPayload.model_validate_json(body)
    except ValidationError as exc:
        raise WebhookMalformedError(
            "the alert does not match the webhook contract", errors=_schema_errors(exc)
        ) from exc

    occurred_at = ensure_utc(payload.timestamp)
    if now - occurred_at > max_age:
        raise WebhookStaleError(
            f"the alert fired at {occurred_at.isoformat()}, more than "
            f"{int(max_age.total_seconds())} s before it was received"
        )
    if occurred_at - now > max_future:
        raise WebhookStaleError(
            f"the alert claims {occurred_at.isoformat()}, more than "
            f"{int(max_future.total_seconds())} s in the future"
        )

    exchange, tradingsymbol = payload.symbol.split(":", 1)
    return ExternalAlert(
        source=SOURCE,
        event_id=payload.alert_id,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        action=payload.action,
        occurred_at=occurred_at,
        received_at=now,
        note=payload.note,
    )

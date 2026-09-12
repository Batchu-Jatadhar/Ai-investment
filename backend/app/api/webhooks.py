"""Inbound alert webhooks.

Authorised by the architecture's ``web`` process, which is the "webhook
gateway" (docs/architecture.md, process table).

``POST /webhooks/tradingview`` is the only write endpoint in the application. It
accepts an authenticated, fresh, never-seen TradingView alert, records it and
answers 202. That is all it does: it places no order, starts no execution,
feeds no strategy and supplies no market data. Accepted alerts are advisory
events in the ledger and nothing more.

It works only in PAPER mode. In any other mode, or with no secret configured,
it refuses before reading the payload.

Checks, in order - so the same request always gets the same answer:

    413 too large -> 403 not paper mode -> 503 no secret configured
    -> 422 malformed JSON -> 401 bad secret -> 422 off-contract
    -> 422 stale / future -> 409 duplicate -> 202 accepted
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from app.adapters.tradingview.webhook import (
    WebhookDuplicateError,
    WebhookModeNotPermittedError,
    WebhookNotConfiguredError,
    WebhookPayloadTooLargeError,
    parse_alert,
)
from app.config.settings import Settings, TradingMode
from app.core.logging import get_logger
from app.core.time import Clock, SystemClock
from app.domain.alerts import AlertLedger
from app.infrastructure.db import get_session_factory
from app.infrastructure.repositories.alerts import SqlAlertLedger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def get_alert_ledger() -> AlertLedger:
    return SqlAlertLedger(get_session_factory())


def get_clock() -> Clock:
    return SystemClock()


class WebhookAccepted(BaseModel):
    status: str
    source: str
    event_id: str
    symbol: str
    action: str


@router.post(
    "/tradingview",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
    summary="Receive a TradingView alert (advisory; paper mode only; places no order)",
)
async def receive_tradingview_alert(
    request: Request,
    ledger: AlertLedger = Depends(get_alert_ledger),
    clock: Clock = Depends(get_clock),
) -> WebhookAccepted:
    settings: Settings = request.app.state.settings
    limit = settings.tradingview_webhook_max_body_bytes

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise WebhookPayloadTooLargeError(f"the body exceeds {limit} bytes")
    body = await request.body()
    if len(body) > limit:
        raise WebhookPayloadTooLargeError(f"the body exceeds {limit} bytes")

    if settings.trading_mode is not TradingMode.PAPER:
        raise WebhookModeNotPermittedError(
            f"trading mode is {settings.trading_mode.value}; alerts are accepted in paper mode only"
        )
    secret = settings.tradingview_webhook_secret
    if secret is None or not secret.get_secret_value():
        raise WebhookNotConfiguredError("TRADINGVIEW_WEBHOOK_SECRET is not set")

    alert = parse_alert(
        body,
        secret=secret.get_secret_value(),
        now=clock.now(),
        max_age=timedelta(seconds=settings.tradingview_webhook_max_age_seconds),
        max_future=timedelta(seconds=settings.tradingview_webhook_max_future_seconds),
    )
    if not ledger.record_if_new(alert):
        raise WebhookDuplicateError(
            f"alert {alert.event_id!r} from {alert.source} was already received"
        )

    logger.info(
        "webhook_alert_accepted",
        extra={
            "source": alert.source,
            "event_id": alert.event_id,
            "symbol": f"{alert.exchange}:{alert.tradingsymbol}",
            "action": alert.action.value,
        },
    )
    return WebhookAccepted(
        status="accepted",
        source=alert.source,
        event_id=alert.event_id,
        symbol=f"{alert.exchange}:{alert.tradingsymbol}",
        action=alert.action.value,
    )

"""API composition root.

Health, read-only market-data queries, and one inbound alert webhook.

There is deliberately NO order-placement route, no broker write route and no
trading route of any kind. The single endpoint that accepts POST is
``/webhooks/tradingview``, authorised by the architecture's ``web`` process as
the webhook gateway: it records advisory alerts and executes nothing. Later
phases attach their routers here; each addition must state which architecture
section authorises it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health, market_data, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(market_data.router)
api_router.include_router(webhooks.router)

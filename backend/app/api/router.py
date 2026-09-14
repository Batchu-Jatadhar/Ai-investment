"""API composition root.

Health, read-only market-data queries, one inbound alert webhook, and the read-only
PAPER dashboard (``/dashboard/paper``, the trader UI read side of paper trading).

There is deliberately NO order-placement route, no broker write route and no
trading route of any kind. The single endpoint that accepts POST is
``/webhooks/tradingview``, authorised by the architecture's ``web`` process as
the webhook gateway: it records advisory alerts and executes nothing. Later
phases attach their routers here; each addition must state which architecture
section authorises it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import dashboard, health, market_data, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(market_data.router)
api_router.include_router(webhooks.router)
api_router.include_router(dashboard.router)

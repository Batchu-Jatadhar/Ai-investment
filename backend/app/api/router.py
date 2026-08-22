"""API composition root.

Read-only. Health plus market-data queries.

There is deliberately NO order-placement route, no broker write route, no
webhook route and no trading route of any kind, and no endpoint in the whole
application accepts POST, PUT, PATCH or DELETE. Later phases attach their
routers here; each addition must state which architecture section authorises it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health, market_data

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(market_data.router)

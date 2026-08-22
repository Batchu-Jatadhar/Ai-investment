"""API composition root.

Phase 0 exposes health only.

There is deliberately NO order-placement route, no broker route, no webhook
route and no trading route of any kind.  Later phases attach their routers
here; each addition must state which architecture section authorises it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)

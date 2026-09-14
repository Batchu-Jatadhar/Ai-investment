"""The PAPER trader dashboard: one read-only GET.

Authorised as the read side of paper trading (architecture: paper phase, trader
UI). It reads the paper session registered in this process and nothing else - no
database, no broker, no order capability. It never returns a fabricated value:
when no session runs, the response says so and every figure is null.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter

from app.config.settings import get_settings
from app.core.time import utc_now
from app.services.paper_dashboard import PaperDashboard, build_paper_dashboard
from app.services.paper_session import get_active_paper_session

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/paper", response_model=PaperDashboard, summary="PAPER trader dashboard")
def paper_dashboard() -> PaperDashboard:
    settings = get_settings()
    session = get_active_paper_session()
    interval = session.params.signal_interval.delta if session is not None else timedelta(0)
    return build_paper_dashboard(
        session,
        trading_mode=settings.trading_mode,
        now=utc_now(),
        # A completed bar is expected every interval; allow one stream-stale window on top.
        stale_after=interval + timedelta(seconds=settings.market_data_stream_stale_seconds),
    )

"""Recording of application lifecycle events.

Deliberately best-effort: the API must start and ``/health/db`` must still be
able to report a database failure even when the database is down.  A failure to
record an event is logged, never raised.
"""

from __future__ import annotations

from typing import Any

from app.config.settings import Settings, get_settings
from app.core.logging import get_correlation_id, get_logger
from app.infrastructure.db import session_scope
from app.infrastructure.models import SystemEvent

logger = get_logger(__name__)


def record_system_event(
    event_type: str,
    settings: Settings | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Append a row to ``system_event``. Returns True when persisted."""
    settings = settings or get_settings()
    try:
        with session_scope() as session:
            session.add(
                SystemEvent(
                    event_type=event_type,
                    app_env=settings.app_env.value,
                    trading_mode=settings.trading_mode.value,
                    app_version=settings.app_version,
                    correlation_id=get_correlation_id(),
                    detail=detail,
                )
            )
        return True
    except Exception as exc:  # noqa: BLE001 - never let bookkeeping break startup
        logger.warning(
            "system_event_not_recorded",
            extra={"event_type": event_type, "error_type": type(exc).__name__},
        )
        return False

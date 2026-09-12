"""SQL implementation of :class:`~app.domain.alerts.AlertLedger`."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.domain.alerts import ExternalAlert
from app.infrastructure.models import WebhookEventRecord
from app.infrastructure.repositories.market_data import SessionFactory

__all__ = ["SqlAlertLedger"]


class SqlAlertLedger:
    """Idempotency by unique constraint.

    The insert either succeeds - the event is new - or violates
    ``uq_webhook_event_source_event``. The database arbitrates, so two
    simultaneous deliveries of one event cannot both be accepted, whichever
    worker receives them.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def record_if_new(self, alert: ExternalAlert) -> bool:
        session = self._session_factory()
        try:
            session.add(
                WebhookEventRecord(
                    source=alert.source,
                    event_id=alert.event_id,
                    exchange=alert.exchange,
                    tradingsymbol=alert.tradingsymbol,
                    action=alert.action.value,
                    occurred_at=alert.occurred_at,
                    received_at=alert.received_at,
                    note=alert.note,
                )
            )
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False
        finally:
            session.close()

"""Inbound alert webhooks: webhook_event.

One row per accepted external alert. The unique (source, event_id) constraint
is what makes webhook delivery idempotent across restarts and API workers.

Portable across PostgreSQL and SQLite (no backend-specific column types).

Revision ID: 0003_webhook_event
Revises: 0002_market_data
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_webhook_event"
down_revision: str | None = "0002_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "webhook_event",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=280), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "event_id", name="uq_webhook_event_source_event"),
    )
    op.create_index("ix_webhook_event_received_at", "webhook_event", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_event_received_at", table_name="webhook_event")
    op.drop_table("webhook_event")

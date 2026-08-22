"""Phase 0 foundation: system_event table.

Deliberately minimal. The full v0.3 schema is not created here; each later
phase adds the tables it actually writes to.

Portable across PostgreSQL and SQLite on purpose (no PG-only column types), so
the same migration runs in local development, in CI against PostgreSQL, and in
the test suite.

Revision ID: 0001_system_event
Revises:
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_system_event"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("app_env", sa.String(length=32), nullable=False),
        sa.Column("trading_mode", sa.String(length=16), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_event_occurred_at", "system_event", ["occurred_at"])
    op.create_index("ix_system_event_event_type", "system_event", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_system_event_event_type", table_name="system_event")
    op.drop_index("ix_system_event_occurred_at", table_name="system_event")
    op.drop_table("system_event")

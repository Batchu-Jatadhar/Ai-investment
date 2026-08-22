"""Phase 1: market-data tables.

instrument, market_tick, candle, connection_event, data_gap, data_quality_event.

Portable across PostgreSQL and SQLite (no backend-specific column types), so the
same migration runs locally, in CI against PostgreSQL, and in the test suite.

Revision ID: 0002_market_data
Revises: 0001_system_event
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_market_data"
down_revision: str | None = "0001_system_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(20, 6)

# SQLite autoincrements only a column declared exactly INTEGER PRIMARY KEY.
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    op.create_table(
        "instrument",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange_token", sa.BigInteger(), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("segment", sa.String(length=32), nullable=False),
        sa.Column("instrument_type", sa.String(length=16), nullable=False),
        sa.Column("tick_size", sa.Numeric(12, 6), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("strike", PRICE, nullable=True),
        sa.Column("last_price", PRICE, nullable=True),
        sa.Column("is_index", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange", "tradingsymbol", name="uq_instrument_symbol"),
    )
    op.create_index("ix_instrument_token", "instrument", ["instrument_token"])
    op.create_index("ix_instrument_segment", "instrument", ["segment"])
    op.create_index("ix_instrument_retrieved_at", "instrument", ["retrieved_at"])

    op.create_table(
        "market_tick",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_price", PRICE, nullable=False),
        sa.Column("last_quantity", sa.BigInteger(), nullable=True),
        sa.Column("average_price", PRICE, nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("total_buy_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_sell_quantity", sa.BigInteger(), nullable=True),
        sa.Column("open_price", PRICE, nullable=True),
        sa.Column("high_price", PRICE, nullable=True),
        sa.Column("low_price", PRICE, nullable=True),
        sa.Column("close_price", PRICE, nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("depth", sa.JSON(), nullable=True),
        sa.Column("is_index", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_tick_token_time", "market_tick", ["instrument_token", "event_time"]
    )
    op.create_index("ix_market_tick_received", "market_tick", ["received_at"])

    op.create_table(
        "candle",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("tick_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("last_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_token", "interval", "start_at", name="uq_candle_bucket"
        ),
    )
    op.create_index(
        "ix_candle_token_interval_start",
        "candle",
        ["instrument_token", "interval", "start_at"],
    )

    op.create_table(
        "connection_event",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connection_event_time", "connection_event", ["occurred_at"])
    op.create_index("ix_connection_event_type", "connection_event", ["event_type"])

    op.create_table(
        "data_gap",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(12, 3), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_data_gap_window", "data_gap", ["started_at", "ended_at"])

    op.create_table(
        "data_quality_event",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_data_quality_time", "data_quality_event", ["occurred_at"])
    op.create_index(
        "ix_data_quality_issue_token",
        "data_quality_event",
        ["issue", "instrument_token"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_quality_issue_token", table_name="data_quality_event")
    op.drop_index("ix_data_quality_time", table_name="data_quality_event")
    op.drop_table("data_quality_event")

    op.drop_index("ix_data_gap_window", table_name="data_gap")
    op.drop_table("data_gap")

    op.drop_index("ix_connection_event_type", table_name="connection_event")
    op.drop_index("ix_connection_event_time", table_name="connection_event")
    op.drop_table("connection_event")

    op.drop_index("ix_candle_token_interval_start", table_name="candle")
    op.drop_table("candle")

    op.drop_index("ix_market_tick_received", table_name="market_tick")
    op.drop_index("ix_market_tick_token_time", table_name="market_tick")
    op.drop_table("market_tick")

    op.drop_index("ix_instrument_retrieved_at", table_name="instrument")
    op.drop_index("ix_instrument_segment", table_name="instrument")
    op.drop_index("ix_instrument_token", table_name="instrument")
    op.drop_table("instrument")

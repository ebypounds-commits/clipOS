"""Transactional outbox and idempotency records: two CREATE-only tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

CREATE-only by policy: 07 requires explicit approval for destructive operations,
and this migration introduces only fresh generic tables.  ``downgrade`` is
therefore a no-op (dropping outbox/idempotency state would destroy data and is
not done here); fresh environments simply start from an empty database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=63), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("business_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_status_available", "outbox_events", ["status", "available_at"], unique=False
    )
    op.create_index("ix_outbox_business_key", "outbox_events", ["business_key"], unique=False)
    op.create_table(
        "idempotency_records",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("business_key", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_idempotency_records_state_locked",
        "idempotency_records",
        ["state", "locked_until"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_records_business_key",
        "idempotency_records",
        ["business_key"],
        unique=True,
        postgresql_where=sa.text("business_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

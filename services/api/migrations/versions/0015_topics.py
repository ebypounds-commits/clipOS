"""Topics, topic aliases, and trend snapshots: CREATE-only tables for CLIP-035.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-09

CREATE-only by policy: this migration introduces only the fresh topic tables.
``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the three topic cluster tables."""
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("display_label", sa.String(length=255), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_topics_org_topic",
        "topics",
        ["org_id", "topic_id"],
        unique=True,
    )
    op.create_index(
        "ix_topics_org_created",
        "topics",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "topic_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_topic_aliases_org_alias",
        "topic_aliases",
        ["org_id", "alias"],
        unique=True,
    )
    op.create_index(
        "ix_topic_aliases_topic",
        "topic_aliases",
        ["topic_id"],
        unique=False,
    )
    op.create_table(
        "trend_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_trend_snapshots_snapshot_id",
        "trend_snapshots",
        ["snapshot_id"],
        unique=True,
    )
    op.create_index(
        "ix_trend_snapshots_topic_time",
        "trend_snapshots",
        ["topic_id", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_trend_snapshots_org_created",
        "trend_snapshots",
        ["org_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

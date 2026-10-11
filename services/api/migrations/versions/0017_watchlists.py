"""Watchlists, watchlist alerts, and trend workflow runs: CREATE-only tables (CLIP-037).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-09

CREATE-only by policy: this migration introduces only the fresh watchlist
tables. ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the three watchlist tables."""
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("watchlist_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_watchlists_org_watch",
        "watchlists",
        ["org_id", "watchlist_id"],
        unique=True,
    )
    op.create_index("ix_watchlists_topic", "watchlists", ["topic_id"], unique=False)
    op.create_table(
        "watchlist_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_watchlist_alerts_alert_id",
        "watchlist_alerts",
        ["alert_id"],
        unique=True,
    )
    op.create_index(
        "uq_watchlist_alerts_run_topic",
        "watchlist_alerts",
        ["run_id", "topic_id"],
        unique=True,
    )
    op.create_index(
        "ix_watchlist_alerts_org",
        "watchlist_alerts",
        ["org_id"],
        unique=False,
    )
    op.create_table(
        "trend_workflow_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_trend_workflow_runs_run_id",
        "trend_workflow_runs",
        ["run_id"],
        unique=True,
    )
    op.create_index(
        "ix_trend_workflow_runs_org",
        "trend_workflow_runs",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

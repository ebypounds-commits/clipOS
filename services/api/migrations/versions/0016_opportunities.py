"""Opportunities, opportunity scores, and decisions: CREATE-only tables (CLIP-036).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09

CREATE-only by policy: this migration introduces only the fresh opportunity
tables. ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the three opportunity tables."""
    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_opportunities_org_opp",
        "opportunities",
        ["org_id", "opportunity_id"],
        unique=True,
    )
    op.create_index(
        "ix_opportunities_topic",
        "opportunities",
        ["topic_id"],
        unique=False,
    )
    op.create_index(
        "ix_opportunities_org_created",
        "opportunities",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "opportunity_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("score_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_opportunity_scores_score_id",
        "opportunity_scores",
        ["score_id"],
        unique=True,
    )
    op.create_index(
        "uq_opportunity_scores_opp",
        "opportunity_scores",
        ["opportunity_id"],
        unique=True,
    )
    op.create_table(
        "opportunity_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("reason_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_opportunity_decisions_id",
        "opportunity_decisions",
        ["decision_id"],
        unique=True,
    )
    op.create_index(
        "uq_opportunity_decisions_opp_kind",
        "opportunity_decisions",
        ["opportunity_id", "decision"],
        unique=True,
    )
    op.create_index(
        "ix_opportunity_decisions_org",
        "opportunity_decisions",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

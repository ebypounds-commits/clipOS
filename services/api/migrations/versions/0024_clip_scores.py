"""Create clip_scores table (CLIP-043).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-12

Create-only migration introducing clip_scores table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the clip_scores table."""
    op.create_table(
        "clip_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("score_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("scorer_alias", sa.String(length=128), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_clip_scores_score_id",
        "clip_scores",
        ["score_id"],
        unique=True,
    )
    op.create_index(
        "uq_clip_scores_org_candidate",
        "clip_scores",
        ["org_id", "candidate_id"],
        unique=True,
    )
    op.create_index("ix_clip_scores_org", "clip_scores", ["org_id"], unique=False)


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

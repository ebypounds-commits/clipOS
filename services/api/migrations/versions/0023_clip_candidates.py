"""Create clip_candidates table (CLIP-042).

Revision ID: 0023
Revises: 0021
Create Date: 2026-10-11

Create-only migration introducing clip_candidates table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0021"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the clip_candidates table."""
    op.create_table(
        "clip_candidates",
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="NEW"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("candidate_id"),
    )
    op.create_index(
        "uq_clip_candidates_org_id_candidate_id",
        "clip_candidates",
        ["org_id", "candidate_id"],
        unique=True,
    )
    op.create_index("ix_clip_candidates_org_id", "clip_candidates", ["org_id"])
    op.create_index("ix_clip_candidates_status", "clip_candidates", ["status"])


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

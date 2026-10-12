"""Create candidate_decisions table (CLIP-045).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-12

Create-only migration introducing candidate_decisions table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the candidate_decisions table."""
    op.create_table(
        "candidate_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("reason_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_candidate_decisions_id",
        "candidate_decisions",
        ["decision_id"],
        unique=True,
    )
    op.create_index(
        "uq_candidate_decisions_candidate_decision",
        "candidate_decisions",
        ["candidate_id", "decision"],
        unique=True,
    )
    op.create_index(
        "ix_candidate_decisions_org",
        "candidate_decisions",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

"""Create factuality_evaluations and transformation_records tables (CLIP-049).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-12

Create-only migration introducing the M11 factuality evaluation and
transformation rights-chain tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create factuality_evaluations and transformation_records tables."""
    op.create_table(
        "factuality_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("eval_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("claim_id", sa.String(length=128), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("method_ref", sa.String(length=128), nullable=False),
        sa.Column("evidence_ref", sa.String(length=512), nullable=True),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_factuality_evaluations_org_eval",
        "factuality_evaluations",
        ["org_id", "eval_id"],
        unique=True,
    )
    op.create_index(
        "ix_factuality_evaluations_org_claim",
        "factuality_evaluations",
        ["org_id", "claim_id"],
        unique=False,
    )

    op.create_table(
        "transformation_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transformation_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("from_ref", sa.String(length=255), nullable=False),
        sa.Column("to_ref", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_transformation_records_org_transformation",
        "transformation_records",
        ["org_id", "transformation_id"],
        unique=True,
    )
    op.create_index(
        "ix_transformation_records_org_brief",
        "transformation_records",
        ["org_id", "brief_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

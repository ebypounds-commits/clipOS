"""Create candidate_fingerprints table (CLIP-044).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-12

Create-only migration introducing candidate_fingerprints table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the candidate_fingerprints table."""
    op.create_table(
        "candidate_fingerprints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("algorithm", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_candidate_fingerprints_org_candidate_kind",
        "candidate_fingerprints",
        ["org_id", "candidate_id", "kind"],
        unique=True,
    )
    op.create_index(
        "ix_candidate_fingerprints_org_kind_value",
        "candidate_fingerprints",
        ["org_id", "kind", "value"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_fingerprints_org",
        "candidate_fingerprints",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

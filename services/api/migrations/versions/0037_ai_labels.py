"""Create ai_labels table (CLIP-057).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-12

Create-only migration introducing the M13 AI-label disclosure table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the ai_labels table."""
    op.create_table(
        "ai_labels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("asset_ref", sa.String(length=512), nullable=False),
        sa.Column("generator_alias", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("detail_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_ai_labels_org_label",
        "ai_labels",
        ["org_id", "label_id"],
        unique=True,
    )
    op.create_index(
        "ix_ai_labels_org_kind",
        "ai_labels",
        ["org_id", "asset_kind"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

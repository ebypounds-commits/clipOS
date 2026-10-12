"""Create voice_assets table (CLIP-054).

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-12

Create-only migration introducing the M13 voice asset table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the voice_assets table."""
    op.create_table(
        "voice_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("voice_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("voice_ref", sa.String(length=255), nullable=False),
        sa.Column("model_alias", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_voice_assets_org_voice",
        "voice_assets",
        ["org_id", "voice_asset_id"],
        unique=True,
    )
    op.create_index("ix_voice_assets_org", "voice_assets", ["org_id"], unique=False)


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

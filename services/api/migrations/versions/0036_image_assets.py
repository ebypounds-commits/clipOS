"""Create image_assets table (CLIP-055).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-12

Create-only migration introducing the M13 generated image asset table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the image_assets table."""
    op.create_table(
        "image_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("image_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.String(length=4000), nullable=False),
        sa.Column("model_alias", sa.String(length=128), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("output_path", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_image_assets_org_image",
        "image_assets",
        ["org_id", "image_asset_id"],
        unique=True,
    )
    op.create_index(
        "ix_image_assets_org_brief",
        "image_assets",
        ["org_id", "brief_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

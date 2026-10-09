"""Media assets and asset versions: CREATE-only tables (CLIP-038).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09

CREATE-only by policy: this migration introduces only the fresh ingestion
tables. ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the two ingestion tables."""
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("source_item_id", sa.String(length=128), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_media_assets_org_asset",
        "media_assets",
        ["org_id", "media_asset_id"],
        unique=True,
    )
    op.create_index(
        "ix_media_assets_source_item",
        "media_assets",
        ["source_item_id"],
        unique=False,
    )
    op.create_table(
        "asset_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_version_id", sa.String(length=128), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.String(length=512), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_asset_versions_version_id",
        "asset_versions",
        ["asset_version_id"],
        unique=True,
    )
    op.create_index(
        "uq_asset_versions_asset_version",
        "asset_versions",
        ["media_asset_id", "version"],
        unique=True,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

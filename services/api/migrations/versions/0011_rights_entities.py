"""Rights & Provenance foundational entities: CREATE-only tables.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh creators,
content_sources, and source_items tables.  ``downgrade`` is therefore a no-op;
fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "creators",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("creator_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("contact_ref", sa.String(length=512), nullable=False),
        sa.Column("creator_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_creators_creator_id", "creators", ["creator_id"], unique=True)
    op.create_index("ix_creators_org_created", "creators", ["org_id", "created_at"], unique=False)
    op.create_table(
        "content_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("creator_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_content_sources_source_id", "content_sources", ["source_id"], unique=True)
    op.create_index(
        "ix_content_sources_org_created",
        "content_sources",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_content_sources_creator", "content_sources", ["creator_id"], unique=False)
    op.create_table(
        "source_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("external_ref", sa.String(length=512), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_ref", sa.String(length=512), nullable=False),
        sa.Column("item_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_source_items_source_external_ref",
        "source_items",
        ["source_id", "external_ref"],
        unique=True,
    )
    op.create_index("uq_source_items_item_id", "source_items", ["item_id"], unique=True)
    op.create_index(
        "ix_source_items_org_captured",
        "source_items",
        ["org_id", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

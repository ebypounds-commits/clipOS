"""Create content_briefs and content_plans tables (CLIP-047).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-12

Create-only migration introducing the M11 content brief and versioned
content plan tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create content_briefs and content_plans tables."""
    op.create_table(
        "content_briefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("research_packet_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.String(length=2048), nullable=False),
        sa.Column("platforms", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_content_briefs_org_brief",
        "content_briefs",
        ["org_id", "brief_id"],
        unique=True,
    )
    op.create_index("ix_content_briefs_org", "content_briefs", ["org_id"], unique=False)

    op.create_table(
        "content_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_content_plans_org_brief_version",
        "content_plans",
        ["org_id", "brief_id", "version"],
        unique=True,
    )
    op.create_index(
        "ix_content_plans_org_brief",
        "content_plans",
        ["org_id", "brief_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

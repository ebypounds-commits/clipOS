"""Create platform_accounts table (CLIP-059).

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-12

Create-only migration introducing the platform account registry table
for channel/account linkage and OAuth scope metadata.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the platform_accounts table."""
    op.create_table(
        "platform_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("account_ref", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("channel_ref", sa.String(length=255), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("account_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_platform_accounts_org_platform_ref",
        "platform_accounts",
        ["org_id", "platform", "account_ref"],
        unique=True,
    )
    op.create_index(
        "ix_platform_accounts_org_platform",
        "platform_accounts",
        ["org_id", "platform"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

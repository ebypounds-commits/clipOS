"""Create hooks, scripts, script_versions tables (CLIP-048).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-12

Create-only migration introducing the M11 hooks and versioned scripts
tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create hooks, scripts, and script_versions tables."""
    op.create_table(
        "hooks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("hook_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("text", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_hooks_org_brief_text",
        "hooks",
        ["org_id", "brief_id", "text"],
        unique=True,
    )
    op.create_index("ix_hooks_org_brief", "hooks", ["org_id", "brief_id"], unique=False)

    op.create_table(
        "scripts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("script_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_scripts_org_script",
        "scripts",
        ["org_id", "script_id"],
        unique=True,
    )
    op.create_index("ix_scripts_org_brief", "scripts", ["org_id", "brief_id"], unique=False)

    op.create_table(
        "script_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.String(length=128), nullable=False),
        sa.Column("script_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.String(length=8192), nullable=False),
        sa.Column("author_ref", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_script_versions_org_script_version",
        "script_versions",
        ["org_id", "script_id", "version"],
        unique=True,
    )
    op.create_index(
        "ix_script_versions_org_script",
        "script_versions",
        ["org_id", "script_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

"""Tool Registry and capability grants: CREATE-only tables.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh tool and
capability-grant tables.  ``downgrade`` is therefore a no-op; fresh
environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "tools",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("capability_scopes", sa.JSON(), nullable=False),
        sa.Column("budget_per_run", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("tool_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_tools_tool_id",
        "tools",
        ["tool_id"],
        unique=True,
    )
    op.create_table(
        "tool_capability_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("tool_id", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_tool_capability_grants_identity",
        "tool_capability_grants",
        ["agent_id", "version", "tool_id"],
        unique=True,
    )
    op.create_index(
        "ix_tool_capability_grants_tool_active",
        "tool_capability_grants",
        ["tool_id", "active"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

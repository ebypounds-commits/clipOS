"""Agent policy overrides and effective-dated budget history: CREATE-only tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07

CREATE-only by policy: this migration introduces only fresh policy and budget
tables.  ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_policy_overrides",
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("policies", sa.JSON(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "version"),
    )
    op.create_table(
        "agent_budget_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_budget_configs_lookup",
        "agent_budget_configs",
        ["agent_id", "version", "effective_from"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

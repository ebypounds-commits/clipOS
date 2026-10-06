"""Agent registry and logical model routing: three CREATE-only tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06

CREATE-only by policy: 07 requires explicit approval for destructive operations,
and this migration introduces only fresh registry tables.  ``downgrade`` is
therefore a no-op (dropping activation and routing state would destroy data and
is not done here); fresh environments simply start from an empty database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_records",
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("active_version", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "agent_versions",
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("agent_ref", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("agent_class", sa.String(length=32), nullable=False),
        sa.Column("prompt_version_ref", sa.String(length=255), nullable=False),
        sa.Column("model_alias", sa.String(length=255), nullable=False),
        sa.Column("fallback_aliases", sa.JSON(), nullable=False),
        sa.Column("required_capabilities", sa.JSON(), nullable=False),
        sa.Column("tool_grants", sa.JSON(), nullable=False),
        sa.Column("prohibited_tools", sa.JSON(), nullable=False),
        sa.Column("memory_policy", sa.JSON(), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("spend_ceilings", sa.JSON(), nullable=False),
        sa.Column("concurrency_ceiling", sa.Integer(), nullable=False),
        sa.Column("evaluators", sa.JSON(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("escalation_policy", sa.JSON(), nullable=False),
        sa.Column("risk_classification", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("canary_percentage", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "version"),
        sa.ForeignKeyConstraint(["agent_ref"], ["agent_records.agent_id"]),
    )
    op.create_index(
        "uq_agent_versions_agent_version",
        "agent_versions",
        ["agent_id", "version"],
        unique=True,
    )
    op.create_index(
        "ix_agent_versions_agent_state",
        "agent_versions",
        ["agent_id", "state"],
        unique=False,
    )
    op.create_table(
        "model_alias_targets",
        sa.Column("target_id", sa.String(length=255), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("endpoint", sa.String(length=255), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_index(
        "uq_model_alias_targets_alias_id",
        "model_alias_targets",
        ["alias", "target_id"],
        unique=True,
    )
    op.create_index(
        "ix_model_alias_targets_alias_priority",
        "model_alias_targets",
        ["alias", "priority"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

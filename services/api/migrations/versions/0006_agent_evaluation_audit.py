"""Agent evaluation runs and execution audit: CREATE-only tables.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07

CREATE-only by policy: this migration introduces only the fresh evaluation
and execution-audit tables.  ``downgrade`` is therefore a no-op; fresh
environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_evaluation_runs",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("evaluator", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "uq_eval_run_identity",
        "agent_evaluation_runs",
        ["agent_id", "version", "evaluator", "run_id"],
        unique=True,
    )
    op.create_table(
        "agent_execution_audits",
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("model_alias", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_class", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("execution_id"),
    )
    op.create_index(
        "ix_exec_audits_agent_time",
        "agent_execution_audits",
        ["agent_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

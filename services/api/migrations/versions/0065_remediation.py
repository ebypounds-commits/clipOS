"""remediation_actions and remediation_runs tables (0064 -> 0065).

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("action_kind", sa.String(64), nullable=False),
        sa.Column("command_template", sa.String(512), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_remediation_actions_org_action",
        "remediation_actions",
        ["org_id", "action_id"],
        unique=True,
    )
    op.create_index(
        "ix_remediation_actions_org_kind",
        "remediation_actions",
        ["org_id", "action_kind"],
        unique=False,
    )
    op.create_table(
        "remediation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("incident_id", sa.String(128), nullable=True),
        sa.Column("rendered_command", sa.String(1024), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_remediation_runs_org_run",
        "remediation_runs",
        ["org_id", "run_id"],
        unique=True,
    )
    op.create_index(
        "ix_remediation_runs_org_action",
        "remediation_runs",
        ["org_id", "action_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_remediation_runs_org_action", table_name="remediation_runs")
    op.drop_index("uq_remediation_runs_org_run", table_name="remediation_runs")
    op.drop_table("remediation_runs")
    op.drop_index("ix_remediation_actions_org_kind", table_name="remediation_actions")
    op.drop_index("uq_remediation_actions_org_action", table_name="remediation_actions")
    op.drop_table("remediation_actions")

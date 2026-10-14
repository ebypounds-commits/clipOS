"""incubation_runs table (0056 -> 0057).

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incubation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("channel_ref", sa.String(255), nullable=False),
        sa.Column("goal_ref", sa.String(512), nullable=True),
        sa.Column("brand_template_id", sa.String(128), nullable=True),
        sa.Column("channel_template_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_incubation_runs_org_run",
        "incubation_runs",
        ["org_id", "run_id"],
        unique=True,
    )
    op.create_index(
        "ix_incubation_runs_org_status",
        "incubation_runs",
        ["org_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_incubation_runs_org_status", table_name="incubation_runs")
    op.drop_index("uq_incubation_runs_org_run", table_name="incubation_runs")
    op.drop_table("incubation_runs")

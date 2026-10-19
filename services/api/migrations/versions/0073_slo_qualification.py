"""slo_objectives, ops_drills (0072 -> 0073).

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slo_objectives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("objective_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("indicator", sa.String(64), nullable=False),
        sa.Column("target_bps", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("min_samples", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_slo_objectives_org_objective",
        "slo_objectives",
        ["org_id", "objective_id"],
        unique=True,
    )
    op.create_index(
        "ix_slo_objectives_org_indicator",
        "slo_objectives",
        ["org_id", "indicator"],
        unique=False,
    )

    op.create_table(
        "ops_drills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("drill_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("control", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("evidence_ref", sa.String(512), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_ops_drills_org_drill", "ops_drills", ["org_id", "drill_id"], unique=True)
    op.create_index("ix_ops_drills_org_control", "ops_drills", ["org_id", "control"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ops_drills_org_control", table_name="ops_drills")
    op.drop_index("uq_ops_drills_org_drill", table_name="ops_drills")
    op.drop_table("ops_drills")
    op.drop_index("ix_slo_objectives_org_indicator", table_name="slo_objectives")
    op.drop_index("uq_slo_objectives_org_objective", table_name="slo_objectives")
    op.drop_table("slo_objectives")

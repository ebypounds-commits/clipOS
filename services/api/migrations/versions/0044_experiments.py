"""experiments and hypotheses tables (0043 -> 0044).

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("target_metric", sa.String(32), nullable=False),
        sa.Column("hypothesis_summary", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_experiments_org_experiment",
        "experiments",
        ["org_id", "experiment_id"],
        unique=True,
    )
    op.create_index(
        "ix_experiments_org_status",
        "experiments",
        ["org_id", "status"],
        unique=False,
    )
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("hypothesis_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("statement", sa.String(2000), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_hypotheses_org_hypothesis",
        "hypotheses",
        ["org_id", "hypothesis_id"],
        unique=True,
    )
    op.create_index(
        "ix_hypotheses_org_experiment",
        "hypotheses",
        ["org_id", "experiment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_hypotheses_org_experiment", table_name="hypotheses")
    op.drop_index("uq_hypotheses_org_hypothesis", table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_index("ix_experiments_org_status", table_name="experiments")
    op.drop_index("uq_experiments_org_experiment", table_name="experiments")
    op.drop_table("experiments")

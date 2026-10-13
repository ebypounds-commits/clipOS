"""cost_anomalies and finance_approvals tables (0054 -> 0055).

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_anomalies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("anomaly_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("entry_id", sa.String(128), nullable=False),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("z_score", sa.Float(), nullable=True),
        sa.Column("mean_cents", sa.Float(), nullable=True),
        sa.Column("stddev_cents", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("detail", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_cost_anomalies_identity",
        "cost_anomalies",
        ["org_id", "anomaly_id"],
        unique=True,
    )
    op.create_index(
        "ix_cost_anomalies_org_entry",
        "cost_anomalies",
        ["org_id", "entry_id"],
        unique=False,
    )
    op.create_table(
        "finance_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("purpose_ref", sa.String(512), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("submitted_by", sa.String(255), nullable=False),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.Column("reason_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_finance_approvals_identity",
        "finance_approvals",
        ["org_id", "approval_id"],
        unique=True,
    )
    op.create_index(
        "ix_finance_approvals_org_status",
        "finance_approvals",
        ["org_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_finance_approvals_org_status", table_name="finance_approvals")
    op.drop_index("uq_finance_approvals_identity", table_name="finance_approvals")
    op.drop_table("finance_approvals")
    op.drop_index("ix_cost_anomalies_org_entry", table_name="cost_anomalies")
    op.drop_index("uq_cost_anomalies_identity", table_name="cost_anomalies")
    op.drop_table("cost_anomalies")

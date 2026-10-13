"""profitability_reports table (0053 -> 0054).

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profitability_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("revenue_cents", sa.BigInteger(), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False),
        sa.Column("profit_cents", sa.BigInteger(), nullable=False),
        sa.Column("margin_pct", sa.Float(), nullable=False),
        sa.Column("profitable", sa.Boolean(), nullable=False),
        sa.Column("reinvest_pct", sa.Float(), nullable=False),
        sa.Column("reinvest_cents", sa.BigInteger(), nullable=False),
        sa.Column("reserve_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_profitability_reports_identity",
        "profitability_reports",
        ["org_id", "report_id"],
        unique=True,
    )
    op.create_index(
        "ix_profitability_reports_org_window",
        "profitability_reports",
        ["org_id", "period_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profitability_reports_org_window",
        table_name="profitability_reports",
    )
    op.drop_index(
        "uq_profitability_reports_identity",
        table_name="profitability_reports",
    )
    op.drop_table("profitability_reports")

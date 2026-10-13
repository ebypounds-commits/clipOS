"""revenue_reconciliations table (0050 -> 0051).

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revenue_reconciliations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reconciliation_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("recorded_cents", sa.BigInteger(), nullable=False),
        sa.Column("expected_cents", sa.BigInteger(), nullable=False),
        sa.Column("delta_cents", sa.BigInteger(), nullable=False),
        sa.Column("delta_pct", sa.Float(), nullable=False),
        sa.Column("tolerance_pct", sa.Float(), nullable=False),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_revenue_reconciliations_identity",
        "revenue_reconciliations",
        ["org_id", "reconciliation_id"],
        unique=True,
    )
    op.create_index(
        "ix_revenue_reconciliations_org_source",
        "revenue_reconciliations",
        ["org_id", "source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_revenue_reconciliations_org_source",
        table_name="revenue_reconciliations",
    )
    op.drop_index(
        "uq_revenue_reconciliations_identity",
        table_name="revenue_reconciliations",
    )
    op.drop_table("revenue_reconciliations")

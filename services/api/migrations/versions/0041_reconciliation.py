"""reconciliation_records table (0040 -> 0041).

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("discrepancies_json", sa.JSON(), nullable=False),
        sa.Column("metric_count", sa.Integer(), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id"),
    )
    op.create_index(
        "ix_reconciliation_records_org_content",
        "reconciliation_records",
        ["org_id", "content_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_records_org_content", table_name="reconciliation_records")
    op.drop_table("reconciliation_records")

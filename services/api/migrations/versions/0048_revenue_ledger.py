"""revenue_sources and revenue_entries tables (0047 -> 0048).

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revenue_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(255), nullable=False),
        sa.Column("revenue_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_revenue_sources_org_source",
        "revenue_sources",
        ["org_id", "source_id"],
        unique=True,
    )
    op.create_index(
        "ix_revenue_sources_org_platform",
        "revenue_sources",
        ["org_id", "platform"],
        unique=False,
    )
    op.create_table(
        "revenue_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("external_ref", sa.String(255), nullable=False),
        sa.Column("revenue_kind", sa.String(16), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id"),
    )
    op.create_index(
        "uq_revenue_entries_identity",
        "revenue_entries",
        ["org_id", "source_id", "external_ref"],
        unique=True,
    )
    op.create_index(
        "ix_revenue_entries_org_source",
        "revenue_entries",
        ["org_id", "source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_revenue_entries_org_source", table_name="revenue_entries")
    op.drop_index("uq_revenue_entries_identity", table_name="revenue_entries")
    op.drop_table("revenue_entries")
    op.drop_index("ix_revenue_sources_org_platform", table_name="revenue_sources")
    op.drop_index("uq_revenue_sources_org_source", table_name="revenue_sources")
    op.drop_table("revenue_sources")

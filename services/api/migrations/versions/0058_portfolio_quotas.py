"""quota_policies and portfolio_allocations tables (0057 -> 0058).

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quota_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_ref", sa.String(255), nullable=False),
        sa.Column("window", sa.String(16), nullable=False),
        sa.Column("max_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_quota_policies_org_quota",
        "quota_policies",
        ["org_id", "quota_id"],
        unique=True,
    )
    op.create_index(
        "ix_quota_policies_org_scope",
        "quota_policies",
        ["org_id", "scope_ref"],
        unique=False,
    )
    op.create_table(
        "portfolio_allocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("allocation_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("allocation", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_portfolio_allocations_identity",
        "portfolio_allocations",
        ["org_id", "allocation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_portfolio_allocations_identity",
        table_name="portfolio_allocations",
    )
    op.drop_table("portfolio_allocations")
    op.drop_index("ix_quota_policies_org_scope", table_name="quota_policies")
    op.drop_index("uq_quota_policies_org_quota", table_name="quota_policies")
    op.drop_table("quota_policies")

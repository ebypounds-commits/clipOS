"""brand_lifecycles and shutdown_policies tables (0058 -> 0059).

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_lifecycles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lifecycle_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("brand_template_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_brand_lifecycles_org_lifecycle",
        "brand_lifecycles",
        ["org_id", "lifecycle_id"],
        unique=True,
    )
    op.create_index(
        "uq_brand_lifecycles_org_brand",
        "brand_lifecycles",
        ["org_id", "brand_template_id"],
        unique=True,
    )
    op.create_table(
        "shutdown_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("brand_template_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("grace_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_shutdown_policies_org_policy",
        "shutdown_policies",
        ["org_id", "policy_id"],
        unique=True,
    )
    op.create_index(
        "uq_shutdown_policies_org_brand",
        "shutdown_policies",
        ["org_id", "brand_template_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_shutdown_policies_org_brand",
        table_name="shutdown_policies",
    )
    op.drop_index(
        "uq_shutdown_policies_org_policy",
        table_name="shutdown_policies",
    )
    op.drop_table("shutdown_policies")
    op.drop_index(
        "uq_brand_lifecycles_org_brand",
        table_name="brand_lifecycles",
    )
    op.drop_index(
        "uq_brand_lifecycles_org_lifecycle",
        table_name="brand_lifecycles",
    )
    op.drop_table("brand_lifecycles")

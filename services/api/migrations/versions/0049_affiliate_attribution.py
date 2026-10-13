"""affiliate_links, affiliate_clicks, affiliate_conversions tables (0048 -> 0049).

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "affiliate_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("link_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("campaign_ref", sa.String(512), nullable=False),
        sa.Column("destination_ref", sa.String(512), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_affiliate_links_org_link",
        "affiliate_links",
        ["org_id", "link_id"],
        unique=True,
    )
    op.create_table(
        "affiliate_clicks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("click_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("link_id", sa.String(128), nullable=False),
        sa.Column("visitor_ref", sa.String(255), nullable=False),
        sa.Column("referrer_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_affiliate_clicks_org_click",
        "affiliate_clicks",
        ["org_id", "click_id"],
        unique=True,
    )
    op.create_index(
        "ix_affiliate_clicks_org_link",
        "affiliate_clicks",
        ["org_id", "link_id"],
        unique=False,
    )
    op.create_table(
        "affiliate_conversions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversion_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("link_id", sa.String(128), nullable=False),
        sa.Column("click_ref", sa.String(128), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("order_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_affiliate_conversions_org_conversion",
        "affiliate_conversions",
        ["org_id", "conversion_id"],
        unique=True,
    )
    op.create_index(
        "ix_affiliate_conversions_org_link",
        "affiliate_conversions",
        ["org_id", "link_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_affiliate_conversions_org_link",
        table_name="affiliate_conversions",
    )
    op.drop_index(
        "uq_affiliate_conversions_org_conversion",
        table_name="affiliate_conversions",
    )
    op.drop_table("affiliate_conversions")
    op.drop_index("ix_affiliate_clicks_org_link", table_name="affiliate_clicks")
    op.drop_index("uq_affiliate_clicks_org_click", table_name="affiliate_clicks")
    op.drop_table("affiliate_clicks")
    op.drop_index("uq_affiliate_links_org_link", table_name="affiliate_links")
    op.drop_table("affiliate_links")

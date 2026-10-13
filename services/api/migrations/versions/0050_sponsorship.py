"""sponsor_campaigns, sponsor_deliverables, sponsor_attributions tables (0049 -> 0050).

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sponsor_campaigns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("sponsor_ref", sa.String(255), nullable=False),
        sa.Column("contracted_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_sponsor_campaigns_org_campaign",
        "sponsor_campaigns",
        ["org_id", "campaign_id"],
        unique=True,
    )
    op.create_table(
        "sponsor_deliverables",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deliverable_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.String(128), nullable=False),
        sa.Column("deliverable_kind", sa.String(64), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_sponsor_deliverables_org_deliverable",
        "sponsor_deliverables",
        ["org_id", "deliverable_id"],
        unique=True,
    )
    op.create_index(
        "ix_sponsor_deliverables_org_campaign",
        "sponsor_deliverables",
        ["org_id", "campaign_id"],
        unique=False,
    )
    op.create_table(
        "sponsor_attributions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("attribution_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.String(128), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("revenue_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_sponsor_attributions_org_attribution",
        "sponsor_attributions",
        ["org_id", "attribution_id"],
        unique=True,
    )
    op.create_index(
        "ix_sponsor_attributions_org_campaign",
        "sponsor_attributions",
        ["org_id", "campaign_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sponsor_attributions_org_campaign",
        table_name="sponsor_attributions",
    )
    op.drop_index(
        "uq_sponsor_attributions_org_attribution",
        table_name="sponsor_attributions",
    )
    op.drop_table("sponsor_attributions")
    op.drop_index(
        "ix_sponsor_deliverables_org_campaign",
        table_name="sponsor_deliverables",
    )
    op.drop_index(
        "uq_sponsor_deliverables_org_deliverable",
        table_name="sponsor_deliverables",
    )
    op.drop_table("sponsor_deliverables")
    op.drop_index(
        "uq_sponsor_campaigns_org_campaign",
        table_name="sponsor_campaigns",
    )
    op.drop_table("sponsor_campaigns")

"""brand_templates and channel_templates tables (0055 -> 0056).

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("voice_ref", sa.String(255), nullable=True),
        sa.Column("color_palette", sa.JSON(), nullable=False),
        sa.Column("logo_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_brand_templates_org_template",
        "brand_templates",
        ["org_id", "template_id"],
        unique=True,
    )
    op.create_table(
        "channel_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("default_visibility", sa.String(16), nullable=False),
        sa.Column("caption_text", sa.String(1000), nullable=False),
        sa.Column("hashtags", sa.JSON(), nullable=False),
        sa.Column("brand_template_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_channel_templates_org_template",
        "channel_templates",
        ["org_id", "template_id"],
        unique=True,
    )
    op.create_index(
        "ix_channel_templates_org_platform",
        "channel_templates",
        ["org_id", "platform"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_templates_org_platform",
        table_name="channel_templates",
    )
    op.drop_index(
        "uq_channel_templates_org_template",
        table_name="channel_templates",
    )
    op.drop_table("channel_templates")
    op.drop_index(
        "uq_brand_templates_org_template",
        table_name="brand_templates",
    )
    op.drop_table("brand_templates")

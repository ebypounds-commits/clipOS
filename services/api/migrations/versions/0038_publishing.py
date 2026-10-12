"""Create publish_policies and publication_jobs tables (CLIP-058).

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-12

Create-only migration introducing the M14 publishing policy and
publication job tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create publish_policies and publication_jobs tables."""
    op.create_table(
        "publish_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("allowed_visibilities", sa.JSON(), nullable=False),
        sa.Column("public_enabled", sa.Boolean(), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_publish_policies_org_policy",
        "publish_policies",
        ["org_id", "policy_id"],
        unique=True,
    )
    op.create_index(
        "uq_publish_policies_org_platform",
        "publish_policies",
        ["org_id", "platform"],
        unique=True,
    )

    op.create_table(
        "publication_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("account_ref", sa.String(length=255), nullable=False),
        sa.Column("content_ref", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("operating_mode", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision_reason", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("error_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_publication_jobs_id",
        "publication_jobs",
        ["job_id"],
        unique=True,
    )
    op.create_index(
        "uq_publication_jobs_org_idempotency",
        "publication_jobs",
        ["org_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_publication_jobs_org_platform",
        "publication_jobs",
        ["org_id", "platform"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

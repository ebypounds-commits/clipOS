"""Create render_outputs and qc_reports tables (CLIP-053).

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-12

Create-only migration introducing the M12 platform variant outputs and
QC report tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create render_outputs and qc_reports tables."""
    op.create_table(
        "render_outputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("output_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("aspect", sa.String(length=8), nullable=False),
        sa.Column("output_path", sa.String(length=1024), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_render_outputs_org_job_platform",
        "render_outputs",
        ["org_id", "job_id", "platform"],
        unique=True,
    )
    op.create_index(
        "ix_render_outputs_org_job",
        "render_outputs",
        ["org_id", "job_id"],
        unique=False,
    )

    op.create_table(
        "qc_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("output_id", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_qc_reports_org_output",
        "qc_reports",
        ["org_id", "output_id"],
        unique=True,
    )
    op.create_index("ix_qc_reports_org_job", "qc_reports", ["org_id", "job_id"], unique=False)


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

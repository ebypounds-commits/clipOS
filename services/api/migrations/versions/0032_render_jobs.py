"""Create render_jobs table (CLIP-051).

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-12

Create-only migration introducing the M12 render job lifecycle table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the render_jobs table."""
    op.create_table(
        "render_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("render_project_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spec_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("error_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_render_jobs_org_job",
        "render_jobs",
        ["org_id", "job_id"],
        unique=True,
    )
    op.create_index(
        "ix_render_jobs_org_project",
        "render_jobs",
        ["org_id", "render_project_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

"""Create render_projects table (CLIP-050).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-12

Create-only migration introducing the M12 render project (DSL) table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the render_projects table."""
    op.create_table(
        "render_projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("render_project_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_render_projects_org_render",
        "render_projects",
        ["org_id", "render_project_id"],
        unique=True,
    )
    op.create_index(
        "ix_render_projects_org_brief",
        "render_projects",
        ["org_id", "brief_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

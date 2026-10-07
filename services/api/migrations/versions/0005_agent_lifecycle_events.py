"""Agent lifecycle event history: CREATE-only table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07

CREATE-only by policy: this migration introduces only the fresh lifecycle
event table.  ``downgrade`` is therefore a no-op; fresh environments start
empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_lifecycle_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=False),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_lifecycle_events_lookup",
        "agent_lifecycle_events",
        ["agent_id", "version", "at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

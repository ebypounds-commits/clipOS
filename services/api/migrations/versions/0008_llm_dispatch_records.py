"""LLM Gateway dispatch ledger: CREATE-only table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07

CREATE-only by policy: this migration introduces only the fresh dispatch-record
table.  ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_dispatch_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("model_ref", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("cost_estimate", sa.Float(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_llm_dispatch_records_identity",
        "llm_dispatch_records",
        ["execution_id", "provider", "model_ref"],
        unique=True,
    )
    op.create_index(
        "ix_llm_dispatch_records_alias_at",
        "llm_dispatch_records",
        ["alias", "at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

"""Create research_packets, claims, claim_sources tables (CLIP-046).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-12

Create-only migration introducing the M11 research packet and
claim/source model tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create research_packets, claims, and claim_sources tables."""
    op.create_table(
        "research_packets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("research_packet_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_research_packets_org_packet",
        "research_packets",
        ["org_id", "research_packet_id"],
        unique=True,
    )
    op.create_index("ix_research_packets_org", "research_packets", ["org_id"], unique=False)

    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("research_packet_id", sa.String(length=128), nullable=False),
        sa.Column("statement", sa.String(length=2048), nullable=False),
        sa.Column("veracity", sa.String(length=16), nullable=False, server_default="UNVERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_claims_org_claim", "claims", ["org_id", "claim_id"], unique=True)
    op.create_index(
        "ix_claims_org_packet",
        "claims",
        ["org_id", "research_packet_id"],
        unique=False,
    )

    op.create_table(
        "claim_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_source_id", sa.String(length=128), nullable=False),
        sa.Column("claim_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("support", sa.String(length=16), nullable=False),
        sa.Column("quote_ref", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_claim_sources_claim_ref",
        "claim_sources",
        ["claim_id", "source_ref"],
        unique=True,
    )
    op.create_index("ix_claim_sources_org", "claim_sources", ["org_id"], unique=False)


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

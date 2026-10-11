"""Semantic segmentation and embedding tables (CLIP-041).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-11

CREATE-only migration: introduces semantic_segments and segment_embeddings tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the semantic_segments and segment_embeddings tables."""
    op.create_table(
        "semantic_segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("segment_pk_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_semantic_segments_org_id",
        "semantic_segments",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        "ix_semantic_segments_kind",
        "semantic_segments",
        ["kind"],
        unique=False,
    )
    op.create_index(
        "uq_semantic_segments_org_id_pk",
        "semantic_segments",
        ["org_id", "segment_pk_id"],
        unique=True,
    )

    op.create_table(
        "segment_embeddings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("embedding_id", sa.String(length=36), nullable=False),
        sa.Column("segment_pk_id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("model_alias", sa.String(length=128), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector_ref", sa.String(length=512), nullable=False),
        sa.Column("norm", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_segment_embeddings_embed_id",
        "segment_embeddings",
        ["embedding_id"],
        unique=True,
    )
    op.create_index(
        "uq_segment_embeddings_seg_model",
        "segment_embeddings",
        ["segment_pk_id", "model_alias"],
        unique=True,
    )
    op.create_index(
        "ix_segment_embeddings_org_id",
        "segment_embeddings",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""

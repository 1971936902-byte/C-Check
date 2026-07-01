"""Add file chunk and embedding caches for incremental RAG indexing."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_rag_incremental_cache"
down_revision = "0014_merge_embedding_candidate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_parse_cache", sa.Column("chunks_json", sa.JSON(), nullable=True))
    op.create_table(
        "code_embedding_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_signature", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_code_embedding_cache_key",
        "code_embedding_cache",
        ["content_hash", "embedding_signature"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("code_embedding_cache")
    op.drop_column("code_parse_cache", "chunks_json")

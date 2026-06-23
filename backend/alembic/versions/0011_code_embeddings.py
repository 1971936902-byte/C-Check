"""Add code embedding metadata table."""

from alembic import op
import sqlalchemy as sa


revision = "0011_code_embeddings"
down_revision = "0010_review_rag_evidence"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("code_embeddings"):
        return
    op.create_table(
        "code_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("vector_id", sa.String(length=128), nullable=False),
        sa.Column("vector_hash", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["code_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_code_embeddings_project_model", "code_embeddings", ["project_id", "embedding_model"])
    op.create_index("ix_code_embeddings_chunk_model", "code_embeddings", ["chunk_id", "embedding_model"])
    op.create_index("ix_code_embeddings_vector_id", "code_embeddings", ["vector_id"])


def downgrade() -> None:
    if _has_table("code_embeddings"):
        op.drop_table("code_embeddings")

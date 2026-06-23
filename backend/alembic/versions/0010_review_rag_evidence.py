"""Add persisted RAG review contexts and evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0010_review_rag_evidence"
down_revision = "0009_code_rag_index"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("review_contexts"):
        op.create_table(
            "review_contexts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=True),
            sa.Column("context_text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["review_tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_review_contexts_task_id", "review_contexts", ["task_id"])
        op.create_index("ix_review_contexts_project_id", "review_contexts", ["project_id"])

    if not _has_table("review_evidence"):
        op.create_table(
            "review_evidence",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("context_id", sa.String(length=36), nullable=False),
            sa.Column("chunk_id", sa.String(length=36), nullable=True),
            sa.Column("evidence_key", sa.String(length=32), nullable=False),
            sa.Column("file_path", sa.String(length=512), nullable=False),
            sa.Column("symbol_name", sa.String(length=255), nullable=True),
            sa.Column("start_line", sa.Integer(), nullable=False),
            sa.Column("end_line", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["review_tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["context_id"], ["review_contexts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["chunk_id"], ["code_chunks.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_review_evidence_context_id", "review_evidence", ["context_id"])
        op.create_index("ix_review_evidence_task_key", "review_evidence", ["task_id", "evidence_key"])
        op.create_index("ix_review_evidence_chunk_id", "review_evidence", ["chunk_id"])


def downgrade() -> None:
    for table_name in ("review_evidence", "review_contexts"):
        if _has_table(table_name):
            op.drop_table(table_name)

"""Add code graph RAG index tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0009_code_rag_index"
down_revision = "0008_model_gpu_metadata"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("code_projects"):
        op.create_table(
            "code_projects",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("source_hash", sa.String(length=64), nullable=False),
            sa.Column("parser_version", sa.String(length=64), nullable=False),
            sa.Column("embedding_backend", sa.String(length=64), nullable=True),
            sa.Column("stats_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["review_tasks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id"),
        )
        op.create_index("ix_code_projects_task_id", "code_projects", ["task_id"])
        op.create_index("ix_code_projects_source_hash", "code_projects", ["source_hash"])

    if not _has_table("code_files"):
        op.create_table(
            "code_files",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("review_file_id", sa.String(length=36), nullable=True),
            sa.Column("relative_path", sa.String(length=512), nullable=False),
            sa.Column("language", sa.String(length=32), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("line_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["review_file_id"], ["review_files.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_code_files_project_path", "code_files", ["project_id", "relative_path"], unique=True)
        op.create_index("ix_code_files_content_hash", "code_files", ["content_hash"])

    if not _has_table("code_symbols"):
        op.create_table(
            "code_symbols",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("file_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("signature", sa.Text(), nullable=True),
            sa.Column("scope", sa.String(length=32), nullable=False),
            sa.Column("start_line", sa.Integer(), nullable=False),
            sa.Column("end_line", sa.Integer(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_tool", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["file_id"], ["code_files.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_code_symbols_project_name", "code_symbols", ["project_id", "name"])
        op.create_index("ix_code_symbols_project_kind", "code_symbols", ["project_id", "kind"])
        op.create_index("ix_code_symbols_file_range", "code_symbols", ["file_id", "start_line", "end_line"])

    if not _has_table("code_edges"):
        op.create_table(
            "code_edges",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("source_id", sa.String(length=36), nullable=False),
            sa.Column("target_id", sa.String(length=36), nullable=True),
            sa.Column("edge_type", sa.String(length=64), nullable=False),
            sa.Column("line", sa.Integer(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_tool", sa.String(length=64), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_code_edges_project_type", "code_edges", ["project_id", "edge_type"])
        op.create_index("ix_code_edges_source", "code_edges", ["source_id"])
        op.create_index("ix_code_edges_target", "code_edges", ["target_id"])

    if not _has_table("code_chunks"):
        op.create_table(
            "code_chunks",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("file_id", sa.String(length=36), nullable=False),
            sa.Column("symbol_id", sa.String(length=36), nullable=True),
            sa.Column("chunk_kind", sa.String(length=32), nullable=False),
            sa.Column("symbol_name", sa.String(length=255), nullable=True),
            sa.Column("start_line", sa.Integer(), nullable=False),
            sa.Column("end_line", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=False),
            sa.Column("embedding_id", sa.String(length=128), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["code_projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["file_id"], ["code_files.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["symbol_id"], ["code_symbols.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_code_chunks_project_symbol", "code_chunks", ["project_id", "symbol_id"])
        op.create_index("ix_code_chunks_project_kind", "code_chunks", ["project_id", "chunk_kind"])
        op.create_index("ix_code_chunks_content_hash", "code_chunks", ["content_hash"])


def downgrade() -> None:
    for table_name in ("code_chunks", "code_edges", "code_symbols", "code_files", "code_projects"):
        if _has_table(table_name):
            op.drop_table(table_name)

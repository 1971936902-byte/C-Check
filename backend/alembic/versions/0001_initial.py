"""Create initial C-Check tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "model_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("model_identifier", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("api_key", sa.String(length=512), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_nodes_is_enabled", "model_nodes", ["is_enabled"])

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("creator_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index("ix_prompt_versions_is_active", "prompt_versions", ["is_active"])

    op.create_table(
        "review_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("model_node_id", sa.String(length=36), nullable=False),
        sa.Column("input_mode", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "completed", "failed", native_enum=False),
            nullable=False,
        ),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["model_node_id"], ["model_nodes.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_tasks_owner_id", "review_tasks", ["owner_id"])
    op.create_index("ix_review_tasks_model_node_id", "review_tasks", ["model_node_id"])
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])
    op.create_index("ix_review_tasks_created_at", "review_tasks", ["created_at"])

    op.create_table(
        "review_files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["task_id"], ["review_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_files_task_id", "review_files", ["task_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("high_count", sa.Integer(), nullable=False),
        sa.Column("medium_count", sa.Integer(), nullable=False),
        sa.Column("low_count", sa.Integer(), nullable=False),
        sa.Column("suggestion_count", sa.Integer(), nullable=False),
        sa.Column("category_counts", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["task_id"], ["review_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("reports")
    op.drop_index("ix_review_files_task_id", table_name="review_files")
    op.drop_table("review_files")
    op.drop_index("ix_review_tasks_created_at", table_name="review_tasks")
    op.drop_index("ix_review_tasks_status", table_name="review_tasks")
    op.drop_index("ix_review_tasks_model_node_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_owner_id", table_name="review_tasks")
    op.drop_table("review_tasks")
    op.drop_index("ix_prompt_versions_is_active", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_model_nodes_is_enabled", table_name="model_nodes")
    op.drop_table("model_nodes")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")

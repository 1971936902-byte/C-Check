"""add review queue priority

Revision ID: 0006_review_queue_priority
Revises: 0005_model_deployments
Create Date: 2026-06-11 20:20:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0006_review_queue_priority"
down_revision: str | None = "0005_model_deployments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("review_tasks")}
    if "queue_priority" not in columns:
        op.add_column(
            "review_tasks",
            sa.Column("queue_priority", sa.Integer(), nullable=False, server_default="0"),
        )
    indexes = {index["name"] for index in inspector.get_indexes("review_tasks")}
    if "ix_review_tasks_queue_priority" not in indexes:
        op.create_index("ix_review_tasks_queue_priority", "review_tasks", ["queue_priority"])


def downgrade() -> None:
    op.drop_index("ix_review_tasks_queue_priority", table_name="review_tasks")
    op.drop_column("review_tasks", "queue_priority")

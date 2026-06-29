"""Persist first-stage candidate JSONL.

Revision ID: 0011_review_candidate_jsonl
Revises: 0010_review_rag_evidence
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0011_review_candidate_jsonl"
down_revision = "0010_review_rag_evidence"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("review_tasks", "candidate_jsonl"):
        op.add_column(
            "review_tasks",
            sa.Column(
                "candidate_jsonl",
                sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    if _has_column("review_tasks", "candidate_jsonl"):
        op.drop_column("review_tasks", "candidate_jsonl")

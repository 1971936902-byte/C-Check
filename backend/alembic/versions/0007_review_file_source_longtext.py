"""use longtext for review file source text

Revision ID: 0007_review_file_source_longtext
Revises: 0006_review_queue_priority
Create Date: 2026-06-13 16:55:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0007_review_file_source_longtext"
down_revision: str | None = "0006_review_queue_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "review_files",
        "source_text",
        existing_type=sa.Text(),
        type_=mysql.LONGTEXT(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "review_files",
        "source_text",
        existing_type=mysql.LONGTEXT(),
        type_=sa.Text(),
        existing_nullable=False,
    )

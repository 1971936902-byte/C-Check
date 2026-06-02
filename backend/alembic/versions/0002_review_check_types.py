"""Add selected review check types.

Revision ID: 0002_review_check_types
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_review_check_types"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_tasks", sa.Column("check_types", sa.JSON(), nullable=True))
    op.execute("UPDATE review_tasks SET check_types = '[]' WHERE check_types IS NULL")
    op.alter_column("review_tasks", "check_types", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    op.drop_column("review_tasks", "check_types")

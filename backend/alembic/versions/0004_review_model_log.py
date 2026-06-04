"""add model log to review tasks

Revision ID: 0004_review_model_log
Revises: 0003_model_default
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_review_model_log"
down_revision = "0003_model_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("review_tasks")}
    if "model_log" not in columns:
        op.add_column("review_tasks", sa.Column("model_log", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_tasks", "model_log")

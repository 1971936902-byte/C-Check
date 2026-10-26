"""Persist first-stage planned-file completion, independent of task percentage."""

from alembic import op
import sqlalchemy as sa

revision = "0017_review_scan_progress"
down_revision = "0016_manual_graph_build"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_tasks", sa.Column("scan_file_total", sa.Integer(), nullable=True))
    op.add_column("review_tasks", sa.Column("scan_file_completed", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_tasks", "scan_file_completed")
    op.drop_column("review_tasks", "scan_file_total")

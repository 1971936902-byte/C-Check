"""Track explicitly requested graph builds separately from reviews."""

from alembic import op
import sqlalchemy as sa

revision = "0016_manual_graph_build"
down_revision = "0015_rag_incremental_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_tasks", sa.Column("graph_build_status", sa.String(16), server_default="idle", nullable=False))
    op.add_column("review_tasks", sa.Column("graph_build_id", sa.String(36), nullable=True))
    op.add_column("review_tasks", sa.Column("graph_build_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_tasks", "graph_build_error")
    op.drop_column("review_tasks", "graph_build_id")
    op.drop_column("review_tasks", "graph_build_status")

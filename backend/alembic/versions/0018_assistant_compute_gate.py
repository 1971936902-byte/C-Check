"""Serialize assistant GPU work with priority review submissions."""
from alembic import op
import sqlalchemy as sa

revision = "0018_assistant_compute_gate"
down_revision = "0017_review_scan_progress"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "assistant_compute_gate",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("operation", sa.String(16), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("fault", sa.String(255), nullable=True),
    )
    op.bulk_insert(table, [{"id": 1, "generation": 0, "cancel_requested": False}])


def downgrade():
    op.drop_table("assistant_compute_gate")

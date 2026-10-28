"""User-owned assistant conversations."""
from alembic import op
import sqlalchemy as sa

revision = "0019_assistant_conversations"
down_revision = "0018_assistant_compute_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("rag_enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_assistant_conversations_owner_id", "assistant_conversations", ["owner_id"])


def downgrade():
    op.drop_table("assistant_conversations")

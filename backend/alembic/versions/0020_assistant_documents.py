"""Track private assistant uploads and resumable embedding state."""
from alembic import op
import sqlalchemy as sa

revision = "0020_assistant_documents"
down_revision = "0019_assistant_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_assistant_documents_owner_id", "assistant_documents", ["owner_id"])


def downgrade():
    op.drop_table("assistant_documents")

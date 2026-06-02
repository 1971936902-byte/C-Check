"""add global default model flag

Revision ID: 0003_model_default
Revises: 0002_review_check_types
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_model_default"
down_revision = "0002_review_check_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_nodes", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_model_nodes_is_default", "model_nodes", ["is_default"])
    op.execute(
        "UPDATE model_nodes SET is_default = 1 "
        "WHERE id = (SELECT id FROM model_nodes WHERE is_enabled = 1 ORDER BY created_at ASC LIMIT 1)"
    )


def downgrade() -> None:
    op.drop_index("ix_model_nodes_is_default", table_name="model_nodes")
    op.drop_column("model_nodes", "is_default")

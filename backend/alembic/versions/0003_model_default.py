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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("model_nodes")}
    indexes = {index["name"] for index in inspector.get_indexes("model_nodes")}

    if "is_default" not in columns:
        op.add_column("model_nodes", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "ix_model_nodes_is_default" not in indexes:
        op.create_index("ix_model_nodes_is_default", "model_nodes", ["is_default"])

    default_model_id = bind.execute(
        sa.text("SELECT id FROM model_nodes WHERE is_enabled = 1 ORDER BY created_at ASC LIMIT 1")
    ).scalar()
    if default_model_id is not None:
        bind.execute(
            sa.text("UPDATE model_nodes SET is_default = 1 WHERE id = :model_id"),
            {"model_id": default_model_id},
        )


def downgrade() -> None:
    op.drop_index("ix_model_nodes_is_default", table_name="model_nodes")
    op.drop_column("model_nodes", "is_default")

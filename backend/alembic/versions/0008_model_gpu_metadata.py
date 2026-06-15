"""Add GPU metadata to model nodes and deployments."""

from alembic import op
import sqlalchemy as sa


revision = "0008_model_gpu_metadata"
down_revision = "0007_review_file_source_longtext"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("model_nodes", "gpu_indices"):
        op.add_column("model_nodes", sa.Column("gpu_indices", sa.JSON(), nullable=True))
    op.execute("UPDATE model_nodes SET gpu_indices = JSON_ARRAY() WHERE gpu_indices IS NULL")
    op.alter_column("model_nodes", "gpu_indices", existing_type=sa.JSON(), nullable=False)
    if not _has_column("model_nodes", "tensor_parallel_size"):
        op.add_column("model_nodes", sa.Column("tensor_parallel_size", sa.Integer(), nullable=False, server_default="1"))
    if not _has_column("model_deployments", "gpu_indices"):
        op.add_column("model_deployments", sa.Column("gpu_indices", sa.JSON(), nullable=True))
    op.execute("UPDATE model_deployments SET gpu_indices = JSON_ARRAY() WHERE gpu_indices IS NULL")
    op.alter_column("model_deployments", "gpu_indices", existing_type=sa.JSON(), nullable=False)
    if not _has_column("model_deployments", "tensor_parallel_size"):
        op.add_column(
            "model_deployments",
            sa.Column("tensor_parallel_size", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    if _has_column("model_deployments", "tensor_parallel_size"):
        op.drop_column("model_deployments", "tensor_parallel_size")
    if _has_column("model_deployments", "gpu_indices"):
        op.drop_column("model_deployments", "gpu_indices")
    if _has_column("model_nodes", "tensor_parallel_size"):
        op.drop_column("model_nodes", "tensor_parallel_size")
    if _has_column("model_nodes", "gpu_indices"):
        op.drop_column("model_nodes", "gpu_indices")

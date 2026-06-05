"""add model deployment records

Revision ID: 0005_model_deployments
Revises: 0004_review_model_log
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_model_deployments"
down_revision = "0004_review_model_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "model_deployments" in inspector.get_table_names():
        return
    op.create_table(
        "model_deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("catalog_key", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("model_identifier", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_repository", sa.String(length=512), nullable=False),
        sa.Column("served_model_name", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("model_dir", sa.String(length=512), nullable=True),
        sa.Column("service_name", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "manual_required",
                name="modeldeploymentstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("log", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.String(length=36), nullable=True),
        sa.Column("model_node_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_node_id"], ["model_nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_deployments_catalog_key", "model_deployments", ["catalog_key"])
    op.create_index("ix_model_deployments_created_at", "model_deployments", ["created_at"])
    op.create_index("ix_model_deployments_status", "model_deployments", ["status"])


def downgrade() -> None:
    op.drop_index("ix_model_deployments_status", table_name="model_deployments")
    op.drop_index("ix_model_deployments_created_at", table_name="model_deployments")
    op.drop_index("ix_model_deployments_catalog_key", table_name="model_deployments")
    op.drop_table("model_deployments")

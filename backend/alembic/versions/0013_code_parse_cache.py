"""Add persistent code parse cache."""

from alembic import op
import sqlalchemy as sa


revision = "0013_code_parse_cache"
down_revision = "0012_postgres_code_search_indexes"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("code_parse_cache"):
        return
    op.create_table(
        "code_parse_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("settings_hash", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("parsed_json", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_code_parse_cache_key",
        "code_parse_cache",
        ["content_hash", "parser_version", "settings_hash"],
        unique=True,
    )


def downgrade() -> None:
    if _has_table("code_parse_cache"):
        op.drop_table("code_parse_cache")

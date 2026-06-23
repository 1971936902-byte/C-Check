"""Add optional PostgreSQL code search indexes."""

from alembic import op


revision = "0012_postgres_code_search_indexes"
down_revision = "0011_code_embeddings"
branch_labels = None
depends_on = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_symbols_name_trgm "
        "ON code_symbols USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_chunks_content_tsv "
        "ON code_chunks USING gin (to_tsvector('simple', content))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_chunks_content_trgm "
        "ON code_chunks USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.execute("DROP INDEX IF EXISTS ix_code_chunks_content_trgm")
    op.execute("DROP INDEX IF EXISTS ix_code_chunks_content_tsv")
    op.execute("DROP INDEX IF EXISTS ix_code_symbols_name_trgm")

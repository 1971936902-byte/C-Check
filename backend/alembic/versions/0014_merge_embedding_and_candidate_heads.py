"""Merge the embedding and candidate JSONL migration branches."""

from __future__ import annotations


revision = "0014_merge_embedding_candidate"
down_revision = ("0013_code_parse_cache", "0011_review_candidate_jsonl")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import CodeChunk, CodeProject, CodeSymbol


@dataclass(frozen=True)
class KeywordHit:
    chunk: CodeChunk
    score: float
    reason: str


def keyword_search_chunks(
    db: Session,
    project: CodeProject,
    identifiers: set[str],
    *,
    limit: int,
) -> list[KeywordHit]:
    if not identifiers:
        return []
    symbols = db.scalars(
        select(CodeSymbol).where(
            CodeSymbol.project_id == project.id,
            CodeSymbol.name.in_(identifiers),
        )
    ).all()
    symbol_ids = {symbol.id for symbol in symbols}
    exact_hits = db.scalars(
        select(CodeChunk).where(CodeChunk.project_id == project.id, CodeChunk.symbol_id.in_(symbol_ids))
    ).all() if symbol_ids else []
    fuzzy_terms = [term for term in identifiers if len(term) >= 4][:20]
    fuzzy_hits: list[CodeChunk] = []
    if fuzzy_terms:
        fuzzy_hits = db.scalars(
            select(CodeChunk).where(
                CodeChunk.project_id == project.id,
                or_(*(CodeChunk.content.contains(term) for term in fuzzy_terms)),
            )
        ).all()
    scores: dict[str, KeywordHit] = {}
    for chunk in exact_hits:
        scores[chunk.id] = KeywordHit(chunk=chunk, score=1.25, reason="symbol-exact")
    for chunk in fuzzy_hits:
        if chunk.id not in scores:
            scores[chunk.id] = KeywordHit(chunk=chunk, score=0.65, reason="content-keyword")
    return sorted(scores.values(), key=lambda hit: hit.score, reverse=True)[:limit]

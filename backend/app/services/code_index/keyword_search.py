from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CodeChunk, CodeProject, CodeSymbol


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
_STOPWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "static",
    "const",
    "void",
    "int",
    "char",
    "short",
    "long",
    "float",
    "double",
    "struct",
    "typedef",
}
_HIGH_VALUE_KINDS = {"function", "declaration", "callsite", "macro", "struct", "union", "typedef", "enum", "type"}


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
    expanded_terms = expand_query_terms(identifiers)
    if not expanded_terms:
        return []

    symbols = db.scalars(
        select(CodeSymbol).where(
            CodeSymbol.project_id == project.id,
            CodeSymbol.name.in_(expanded_terms),
        )
    ).all()
    symbol_ids = {symbol.id for symbol in symbols}
    chunks = db.scalars(select(CodeChunk).where(CodeChunk.project_id == project.id)).all()
    if not chunks:
        return []

    bm25_scores = _bm25_scores(chunks, expanded_terms)
    hits: dict[str, KeywordHit] = {}
    for chunk in chunks:
        score = bm25_scores.get(chunk.id, 0.0)
        reasons: list[str] = []
        if chunk.symbol_id in symbol_ids:
            score += 2.4
            reasons.append("symbol-exact")
        if chunk.symbol_name and chunk.symbol_name in identifiers:
            score += 1.2
            reasons.append("target-name")
        if score <= 0:
            continue
        if chunk.chunk_kind in _HIGH_VALUE_KINDS:
            score += 0.15
        if not reasons:
            reasons.append("bm25")
        hits[chunk.id] = KeywordHit(chunk=chunk, score=score, reason="+".join(reasons))
    return sorted(hits.values(), key=lambda hit: hit.score, reverse=True)[:limit]


def expand_query_terms(identifiers: set[str], *, max_terms: int = 80) -> set[str]:
    expanded: set[str] = set()
    for identifier in identifiers:
        for term in _split_identifier(identifier):
            if len(term) > 2 and term not in _STOPWORDS:
                expanded.add(term)
        if len(identifier) > 2 and identifier not in _STOPWORDS:
            expanded.add(identifier)
    return set(sorted(expanded, key=lambda term: (-len(term), term))[:max_terms])


def _split_identifier(identifier: str) -> set[str]:
    parts = {identifier}
    for raw_part in re.split(r"[_\W]+", identifier):
        if not raw_part:
            continue
        parts.add(raw_part)
        parts.update(part for part in _CAMEL_RE.split(raw_part) if part)
    return {part for item in parts for part in {item, item.lower()} if part}


def _bm25_scores(chunks: list[CodeChunk], terms: set[str]) -> dict[str, float]:
    tokenized = {chunk.id: _tokens_for_chunk(chunk) for chunk in chunks}
    doc_count = len(chunks)
    avgdl = sum(len(tokens) for tokens in tokenized.values()) / max(1, doc_count)
    doc_freq: Counter[str] = Counter()
    for tokens in tokenized.values():
        doc_freq.update(set(tokens) & terms)

    k1 = 1.5
    b = 0.75
    scores: dict[str, float] = {}
    for chunk in chunks:
        tokens = tokenized[chunk.id]
        if not tokens:
            continue
        counts = Counter(tokens)
        length = len(tokens)
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if tf <= 0:
                continue
            idf = math.log(1 + (doc_count - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            denom = tf + k1 * (1 - b + b * (length / max(avgdl, 1e-9)))
            score += idf * ((tf * (k1 + 1)) / denom)
        if score > 0:
            scores[chunk.id] = score
    return scores


def _tokens_for_chunk(chunk: CodeChunk) -> list[str]:
    values = [chunk.content, chunk.symbol_name or "", chunk.chunk_kind]
    metadata = chunk.metadata_json if isinstance(chunk.metadata_json, dict) else {}
    for value in metadata.values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    tokens: list[str] = []
    for value in values:
        for token in _TOKEN_RE.findall(value):
            tokens.extend(_split_identifier(token))
    return [token.lower() for token in tokens if len(token) > 2 and token.lower() not in _STOPWORDS]

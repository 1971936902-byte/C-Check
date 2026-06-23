from __future__ import annotations

import re
from dataclasses import dataclass
from operator import attrgetter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeEdge, CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.embeddings import embed_text
from app.services.code_index.indexer import load_or_build_code_index
from app.services.code_index.keyword_search import keyword_search_chunks


_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_COMMON_IDENTIFIERS = {
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


@dataclass(frozen=True)
class RetrievedContext:
    chunk_id: str | None
    evidence_id: str
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int
    content: str
    reason: str
    score: float


def retrieve_context_for_files(
    db: Session,
    task: ReviewTask,
    files: list[ReviewFile],
    *,
    settings: Settings | None = None,
) -> list[RetrievedContext]:
    settings = settings or get_settings()
    if not settings.rag_enabled or not files:
        return []
    project = load_or_build_code_index(db, task, settings=settings)
    target_paths = {file.relative_path for file in files}
    chunks_by_symbol = _chunks_by_symbol(db, project)
    contexts: dict[str, RetrievedContext] = {}

    for source_file in files:
        identifiers = _identifiers(source_file.source_text)
        graph_depth = max(settings.rag_graph_max_depth, 2 if _has_high_risk_api(source_file.source_text) else 1)
        candidate_groups = [
            _include_contexts(db, project, source_file.relative_path),
            _direct_call_contexts(db, project, source_file.relative_path, chunks_by_symbol, max_depth=graph_depth),
            _upstream_contexts(db, project, source_file.relative_path, chunks_by_symbol),
            _usage_contexts(db, project, source_file.relative_path, chunks_by_symbol),
            _keyword_contexts(db, project, identifiers, target_paths, limit=settings.rag_keyword_top_k),
            _vector_contexts(db, project, source_file.source_text, target_paths, limit=settings.rag_keyword_top_k),
        ]
        for group in candidate_groups:
            for context in group:
                current = contexts.get(context.evidence_id)
                if current is None or context.score > current.score:
                    contexts[context.evidence_id] = context

    ranked = sorted(contexts.values(), key=attrgetter("score"), reverse=True)
    return ranked[: settings.rag_keyword_top_k]


def _direct_call_contexts(
    db: Session,
    project: CodeProject,
    relative_path: str,
    chunks_by_symbol: dict[str, CodeChunk],
    *,
    max_depth: int,
) -> list[RetrievedContext]:
    source_ids = _function_symbol_ids_for_file(db, project, relative_path)
    if not source_ids:
        return []
    visited = set(source_ids)
    frontier = set(source_ids)
    all_edges: list[tuple[int, CodeEdge]] = []
    depth = 0
    while frontier and depth < max(1, max_depth):
        edges = _call_edges_from_sources(db, project, frontier)
        all_edges.extend((depth + 1, edge) for edge in edges)
        next_frontier = {edge.target_id for edge in edges if edge.target_id and edge.target_id not in visited}
        visited.update(item for item in next_frontier if item)
        frontier = {item for item in next_frontier if item}
        depth += 1
    contexts: list[RetrievedContext] = []
    for edge_depth, edge in all_edges:
        chunk = chunks_by_symbol.get(edge.target_id or "")
        if chunk:
            contexts.append(_context_from_chunk(chunk, "调用关系", 1.0 + edge.confidence - (0.1 * (edge_depth - 1))))
    return contexts


def _call_edges_from_sources(db: Session, project: CodeProject, source_ids: set[str]) -> list[CodeEdge]:
    if not source_ids:
        return []
    return db.scalars(
        select(CodeEdge).where(
            CodeEdge.project_id == project.id,
            CodeEdge.edge_type == "FUNCTION_CALLS_FUNCTION",
            CodeEdge.source_id.in_(source_ids),
            CodeEdge.target_id.is_not(None),
        )
    ).all()


def _include_contexts(db: Session, project: CodeProject, relative_path: str) -> list[RetrievedContext]:
    file_id = db.scalar(
        select(CodeFile.id).where(CodeFile.project_id == project.id, CodeFile.relative_path == relative_path)
    )
    if not file_id:
        return []
    include_edges = db.scalars(
        select(CodeEdge).where(
            CodeEdge.project_id == project.id,
            CodeEdge.edge_type == "FILE_INCLUDES_FILE",
            CodeEdge.source_id == file_id,
        )
    ).all()
    contexts: list[RetrievedContext] = []
    for edge in include_edges:
        include_name = edge.metadata_json.get("include") if isinstance(edge.metadata_json, dict) else None
        if not include_name:
            continue
        include_file = db.scalar(
            select(CodeFile).where(
                CodeFile.project_id == project.id,
                CodeFile.relative_path.endswith(str(include_name)),
            )
        )
        if include_file is None:
            continue
        chunk = db.scalar(
            select(CodeChunk).where(
                CodeChunk.project_id == project.id,
                CodeChunk.file_id == include_file.id,
                CodeChunk.chunk_kind == "file_summary",
            )
        )
        if chunk:
            contexts.append(_context_from_chunk(chunk, "include关系", 1.25))
    return contexts


def _upstream_contexts(
    db: Session,
    project: CodeProject,
    relative_path: str,
    chunks_by_symbol: dict[str, CodeChunk],
) -> list[RetrievedContext]:
    target_ids = _function_symbol_ids_for_file(db, project, relative_path)
    if not target_ids:
        return []
    edges = db.scalars(
        select(CodeEdge).where(
            CodeEdge.project_id == project.id,
            CodeEdge.edge_type == "FUNCTION_CALLS_FUNCTION",
            CodeEdge.target_id.in_(target_ids),
        )
    ).all()
    return [
        _context_from_chunk(chunk, "上游调用者", 0.9 + edge.confidence)
        for edge in edges
        if (chunk := chunks_by_symbol.get(edge.source_id))
    ]


def _usage_contexts(
    db: Session,
    project: CodeProject,
    relative_path: str,
    chunks_by_symbol: dict[str, CodeChunk],
) -> list[RetrievedContext]:
    source_ids = _function_symbol_ids_for_file(db, project, relative_path)
    if not source_ids:
        return []
    edges = db.scalars(
        select(CodeEdge).where(
            CodeEdge.project_id == project.id,
            CodeEdge.edge_type.in_(
                [
                    "FUNCTION_USES_MACRO",
                    "FUNCTION_USES_TYPE",
                    "FUNCTION_USES_GLOBAL",
                    "SYMBOL_DECLARED_IN",
                    "SYMBOL_DEFINED_IN",
                ]
            ),
            CodeEdge.source_id.in_(source_ids),
            CodeEdge.target_id.is_not(None),
        )
    ).all()
    return [
        _context_from_chunk(chunk, "声明/宏/类型/全局变量", 0.85 + edge.confidence)
        for edge in edges
        if (chunk := chunks_by_symbol.get(edge.target_id or ""))
    ]


def _keyword_contexts(
    db: Session,
    project: CodeProject,
    identifiers: set[str],
    target_paths: set[str],
    *,
    limit: int,
) -> list[RetrievedContext]:
    contexts: list[RetrievedContext] = []
    for hit in keyword_search_chunks(db, project, identifiers, limit=limit):
        chunk = hit.chunk
        if chunk.file and chunk.file.relative_path in target_paths and chunk.chunk_kind == "function":
            continue
        contexts.append(_context_from_chunk(chunk, f"关键词检索:{hit.reason}", 0.7 + hit.score))
    return sorted(contexts, key=attrgetter("score"), reverse=True)[:limit]


def _vector_contexts(
    db: Session,
    project: CodeProject,
    source_text: str,
    target_paths: set[str],
    *,
    limit: int,
) -> list[RetrievedContext]:
    query_vector = embed_text(source_text)
    scored: list[tuple[float, CodeChunk]] = []
    for chunk in db.scalars(select(CodeChunk).where(CodeChunk.project_id == project.id)).all():
        if chunk.file.relative_path in target_paths and chunk.chunk_kind in {"function", "file_summary"}:
            continue
        score = _cosine_similarity(query_vector, embed_text(chunk.content))
        if score >= 0.15:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_context_from_chunk(chunk, "向量相似检索", 0.5 + score) for score, chunk in scored[:limit]]


def _chunks_by_symbol(db: Session, project: CodeProject) -> dict[str, CodeChunk]:
    chunks = db.scalars(
        select(CodeChunk).where(CodeChunk.project_id == project.id, CodeChunk.symbol_id.is_not(None))
    ).all()
    by_symbol: dict[str, CodeChunk] = {}
    for chunk in chunks:
        if chunk.symbol_id and (chunk.symbol_id not in by_symbol or chunk.chunk_kind == "function"):
            by_symbol[chunk.symbol_id] = chunk
    return by_symbol


def _function_symbol_ids_for_file(db: Session, project: CodeProject, relative_path: str) -> set[str]:
    file_id = db.scalar(
        select(CodeFile.id).where(CodeFile.project_id == project.id, CodeFile.relative_path == relative_path)
    )
    if not file_id:
        return set()
    return {
        symbol.id
        for symbol in db.scalars(
            select(CodeSymbol).where(CodeSymbol.project_id == project.id, CodeSymbol.file_id == file_id)
        ).all()
        if symbol.kind == "function"
    }


def _context_from_chunk(chunk: CodeChunk, reason: str, score: float) -> RetrievedContext:
    return RetrievedContext(
        chunk_id=chunk.id,
        evidence_id=f"{chunk.file.relative_path}:{chunk.start_line}:{chunk.end_line}:{chunk.symbol_name or ''}",
        file_path=chunk.file.relative_path,
        symbol_name=chunk.symbol_name,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        reason=reason,
        score=score,
    )


def _identifiers(source_text: str) -> set[str]:
    return {
        match.group(0)
        for match in _IDENTIFIER_RE.finditer(source_text)
        if match.group(0) not in _COMMON_IDENTIFIERS and len(match.group(0)) > 2
    }


def _has_high_risk_api(source_text: str) -> bool:
    return bool(
        re.search(
            r"\b(memcpy|memmove|strcpy|strncpy|sprintf|malloc|calloc|realloc|free|open|close|fopen|fclose|mutex_lock|mutex_unlock)\s*\(",
            source_text,
        )
    )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))

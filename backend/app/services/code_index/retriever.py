from __future__ import annotations

import re
from dataclasses import dataclass
from operator import attrgetter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeEdge, CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.indexer import load_or_build_code_index


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
        for context in _include_contexts(db, project, source_file.relative_path):
            contexts[context.evidence_id] = context
        for context in _direct_call_contexts(db, project, source_file.relative_path, chunks_by_symbol, max_depth=graph_depth):
            contexts[context.evidence_id] = context
        for context in _keyword_contexts(
            db,
            project,
            identifiers,
            target_paths,
            chunks_by_symbol,
            limit=settings.rag_keyword_top_k,
        ):
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
    file_id = db.scalar(
        select(CodeFile.id).where(CodeFile.project_id == project.id, CodeFile.relative_path == relative_path)
    )
    if not file_id:
        return []
    source_symbols = db.scalars(
        select(CodeSymbol).where(CodeSymbol.project_id == project.id, CodeSymbol.file_id == file_id)
    ).all()
    source_ids = {symbol.id for symbol in source_symbols if symbol.kind == "function"}
    if not source_ids:
        return []
    edges = _call_edges_from_sources(db, project, source_ids)
    contexts: list[RetrievedContext] = []
    visited = set(source_ids)
    frontier = set(source_ids)
    depth = 0
    all_edges = []
    while frontier and depth < max(1, max_depth):
        edges = _call_edges_from_sources(db, project, frontier)
        all_edges.extend(edges)
        next_frontier = {edge.target_id for edge in edges if edge.target_id and edge.target_id not in visited}
        visited.update(item for item in next_frontier if item)
        frontier = {item for item in next_frontier if item}
        depth += 1
    for edge in all_edges:
        chunk = chunks_by_symbol.get(edge.target_id or "")
        if not chunk:
            continue
        contexts.append(_context_from_chunk(chunk, "调用关系", 1.0 + edge.confidence - (0.1 * max(0, depth - 1))))
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


def _keyword_contexts(
    db: Session,
    project: CodeProject,
    identifiers: set[str],
    target_paths: set[str],
    chunks_by_symbol: dict[str, CodeChunk],
    *,
    limit: int,
) -> list[RetrievedContext]:
    if not identifiers:
        return []
    symbols = db.scalars(
        select(CodeSymbol).where(CodeSymbol.project_id == project.id, CodeSymbol.name.in_(identifiers))
    ).all()
    contexts: list[RetrievedContext] = []
    for symbol in symbols:
        if symbol.file and symbol.file.relative_path in target_paths and symbol.kind == "function":
            continue
        chunk = chunks_by_symbol.get(symbol.id)
        if not chunk:
            continue
        contexts.append(_context_from_chunk(chunk, "关键字命中", 0.7 + symbol.confidence))
    return sorted(contexts, key=attrgetter("score"), reverse=True)[:limit]


def _chunks_by_symbol(db: Session, project: CodeProject) -> dict[str, CodeChunk]:
    chunks = db.scalars(select(CodeChunk).where(CodeChunk.project_id == project.id, CodeChunk.symbol_id.is_not(None))).all()
    return {chunk.symbol_id: chunk for chunk in chunks if chunk.symbol_id}


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

from __future__ import annotations

import re
from dataclasses import dataclass
from operator import attrgetter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeEdge, CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.embeddings import embed_text_with_settings
from app.services.code_index.indexer import load_or_build_code_index
from app.services.code_index.keyword_search import keyword_search_chunks
from app.services.code_index.qdrant import QdrantCodeIndexClient


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
_LOW_VALUE_RAG_IDENTIFIERS = {
    "CAN",
    "DAC",
    "DMA",
    "DMA1",
    "DMA2",
    "ADC",
    "GPIO",
    "USART",
    "UART",
    "SPI",
    "I2C",
    "RCC",
    "NVIC",
    "EXTI",
    "TIM",
    "USB",
    "NULL",
    "TRUE",
    "FALSE",
    "SET",
    "RESET",
    "ENABLE",
    "DISABLE",
    "SUCCESS",
    "ERROR",
    "u8",
    "u16",
    "u32",
    "s8",
    "s16",
    "s32",
    "uint8_t",
    "uint16_t",
    "uint32_t",
    "int8_t",
    "int16_t",
    "int32_t",
    "size_t",
}
_HIGH_RISK_IDENTIFIERS = {
    "memcpy",
    "memmove",
    "strcpy",
    "strncpy",
    "sprintf",
    "snprintf",
    "scanf",
    "sscanf",
    "malloc",
    "calloc",
    "realloc",
    "free",
    "open",
    "close",
    "fopen",
    "fclose",
    "read",
    "write",
    "recv",
    "send",
    "lock",
    "unlock",
}
_SYMBOL_KIND_WEIGHT = {
    "function": 1.25,
    "function_window": 0.9,
    "declaration": 0.92,
    "callsite": 0.45,
    "macro": 0.82,
    "type": 0.66,
    "struct": 0.66,
    "typedef": 0.66,
    "enum": 0.66,
    "global_variable": 0.52,
    "file_summary": 0.25,
}
REASON_CALL = "call"
REASON_INCLUDE = "include"
REASON_UPSTREAM = "upstream"
REASON_SYMBOL = "symbol"
REASON_KEYWORD = "keyword"
REASON_VECTOR = "vector"
REASON_MISSING = "missing"
RAG_PURPOSE_DEFAULT = "default"
RAG_PURPOSE_CANDIDATE = "candidate"
RAG_PURPOSE_CONFIRMATION = "confirmation"


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
    purpose: str = RAG_PURPOSE_DEFAULT,
) -> list[RetrievedContext]:
    settings = settings or get_settings()
    if not settings.rag_enabled or not files:
        return []
    project = load_or_build_code_index(db, task, settings=settings)
    target_paths = {file.relative_path for file in files}
    chunks_by_symbol = _chunks_by_symbol(db, project)
    contexts: dict[str, RetrievedContext] = {}
    source_text_by_path = {file.relative_path: file.source_text for file in files}

    for source_file in files:
        identifiers = _rag_query_identifiers(source_file.source_text)
        definition_only = _definition_only_mode(settings, purpose)
        graph_depth = 1 if definition_only or settings.rag_on_demand_enabled else max(settings.rag_graph_max_depth, 2 if _has_high_risk_api(source_file.source_text) else 1)
        candidate_groups = [
            _include_contexts(db, project, source_file.relative_path),
            _direct_call_contexts(db, project, source_file.relative_path, chunks_by_symbol, max_depth=graph_depth),
            _usage_contexts(db, project, source_file.relative_path, chunks_by_symbol),
        ]
        if not definition_only:
            candidate_groups.extend(
                [
                    _upstream_contexts(db, project, source_file.relative_path, chunks_by_symbol),
                    _keyword_contexts(db, project, identifiers, target_paths, limit=settings.rag_keyword_top_k),
                ]
            )
            if settings.rag_qdrant_url:
                candidate_groups.append(
                    _qdrant_contexts(db, project, source_file.source_text, target_paths, settings=settings, limit=settings.rag_keyword_top_k)
                )
        if not settings.rag_on_demand_enabled and not definition_only:
            candidate_groups.extend(
                [
                    _vector_contexts(db, project, source_file.source_text, target_paths, settings=settings, limit=settings.rag_keyword_top_k),
                ]
            )
        for group in candidate_groups:
            for context in group:
                current = contexts.get(context.evidence_id)
                if current is None or _context_merge_key(context) > _context_merge_key(current):
                    contexts[context.evidence_id] = context

    profile_contexts = _filter_contexts_for_profile(db, project, list(contexts.values()), target_paths, settings, purpose=purpose)
    ranked = _rerank_contexts(db, project, profile_contexts, target_paths, source_text_by_path)
    return _prune_ranked_contexts(ranked, limit=settings.rag_keyword_top_k, purpose=purpose)


def retrieve_missing_symbol_contexts(
    db: Session,
    task: ReviewTask,
    files: list[ReviewFile],
    *,
    settings: Settings | None = None,
    purpose: str = RAG_PURPOSE_DEFAULT,
) -> list[RetrievedContext]:
    settings = settings or get_settings()
    if not settings.rag_enabled or not files:
        return []
    project = load_or_build_code_index(db, task, settings=settings)
    target_paths = {file.relative_path for file in files}
    source_text_by_path = {file.relative_path: file.source_text for file in files}
    referenced = set().union(*(_referenced_symbols(file.source_text) for file in files))
    locally_defined = set().union(*(_locally_defined_symbols(file.source_text) for file in files))
    missing_names = {
        name
        for name in referenced - locally_defined - _COMMON_IDENTIFIERS
        if not _is_low_value_rag_identifier(name)
    }
    if not missing_names:
        return []

    chunks_by_symbol = _chunks_by_symbol(db, project)
    contexts: dict[str, RetrievedContext] = {}
    symbols = db.scalars(
        select(CodeSymbol).where(
            CodeSymbol.project_id == project.id,
            CodeSymbol.name.in_(missing_names),
        )
    ).all()
    for symbol in symbols:
        if not _definition_symbol_allowed(symbol.kind):
            continue
        chunk = chunks_by_symbol.get(symbol.id)
        if chunk is None:
            continue
        if _definition_only_mode(settings, purpose) and not _definition_chunk_allowed(chunk, target_paths):
            continue
        score = _missing_symbol_score(symbol.kind, symbol.name, chunk.file.relative_path, target_paths)
        context = _context_from_chunk(chunk, f"{REASON_MISSING}:{symbol.kind}", score)
        current = contexts.get(context.evidence_id)
        if current is None or context.score > current.score:
            contexts[context.evidence_id] = context

    if not _definition_only_mode(settings, purpose):
        for hit in keyword_search_chunks(db, project, missing_names, limit=settings.rag_keyword_top_k):
            chunk = hit.chunk
            if chunk.file.relative_path in target_paths and chunk.chunk_kind in {"function", "file_summary"}:
                continue
            if chunk.symbol_name not in missing_names and not (_identifiers(chunk.content) & missing_names):
                continue
            context = _context_from_chunk(chunk, f"{REASON_MISSING}:keyword", 1.0 + hit.score)
            current = contexts.get(context.evidence_id)
            if current is None or context.score > current.score:
                contexts[context.evidence_id] = context

    profile_contexts = _filter_contexts_for_profile(db, project, list(contexts.values()), target_paths, settings, purpose=purpose)
    ranked = _rerank_contexts(db, project, profile_contexts, target_paths, source_text_by_path)
    return _prune_ranked_contexts(ranked, limit=max(settings.rag_keyword_top_k, 6), purpose=purpose)


def retrieve_context_diagnostics(
    db: Session,
    task: ReviewTask,
    files: list[ReviewFile],
    *,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    if not settings.rag_enabled or not files:
        return {"enabled": False, "selected": [], "rejected": []}
    project = load_or_build_code_index(db, task, settings=settings)
    target_paths = {file.relative_path for file in files}
    chunks_by_symbol = _chunks_by_symbol(db, project)
    source_text_by_path = {file.relative_path: file.source_text for file in files}
    contexts: dict[str, RetrievedContext] = {}
    for source_file in files:
        identifiers = _identifiers(source_file.source_text)
        graph_depth = max(settings.rag_graph_max_depth, 2 if _has_high_risk_api(source_file.source_text) else 1)
        for group in (
            _include_contexts(db, project, source_file.relative_path),
            _direct_call_contexts(db, project, source_file.relative_path, chunks_by_symbol, max_depth=graph_depth),
            _upstream_contexts(db, project, source_file.relative_path, chunks_by_symbol),
            _usage_contexts(db, project, source_file.relative_path, chunks_by_symbol),
            _keyword_contexts(db, project, identifiers, target_paths, limit=settings.rag_keyword_top_k),
            _qdrant_contexts(db, project, source_file.source_text, target_paths, settings=settings, limit=settings.rag_keyword_top_k),
            _vector_contexts(db, project, source_file.source_text, target_paths, limit=settings.rag_keyword_top_k, settings=settings),
        ):
            for context in group:
                current = contexts.get(context.evidence_id)
                if current is None or _context_merge_key(context) > _context_merge_key(current):
                    contexts[context.evidence_id] = context
    ranked = _rerank_contexts(db, project, list(contexts.values()), target_paths, source_text_by_path)
    selected = _prune_ranked_contexts(ranked, limit=settings.rag_keyword_top_k)
    selected_ids = {context.evidence_id for context in selected}
    rejected = [context for context in ranked if context.evidence_id not in selected_ids]
    return {
        "enabled": True,
        "project_id": project.id,
        "stats": project.stats_json,
        "target_files": sorted(target_paths),
        "raw_candidate_count": len(contexts),
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "bucket_counts": _bucket_counts(ranked),
        "selected": [_context_to_diagnostic(context) for context in selected],
        "rejected": [_context_to_diagnostic(context) for context in rejected[:20]],
        "budget": {
            "top_k": settings.rag_keyword_top_k,
            "context_max_chars": settings.rag_context_max_chars,
        },
    }


def _rerank_contexts(
    db: Session,
    project: CodeProject,
    contexts: list[RetrievedContext],
    target_paths: set[str],
    source_text_by_path: dict[str, str],
) -> list[RetrievedContext]:
    chunks_by_id = _chunks_by_id(db, project)
    source_identifiers = set().union(*(_identifiers(text) for text in source_text_by_path.values())) if source_text_by_path else set()
    source_has_high_risk_api = any(_has_high_risk_api(text) for text in source_text_by_path.values())
    rescored: list[RetrievedContext] = []
    for context in contexts:
        chunk = chunks_by_id.get(context.chunk_id or "")
        chunk_kind = chunk.chunk_kind if chunk else _kind_from_reason(context.reason)
        relation_boost = _relation_boost(context.reason)
        symbol_boost = _SYMBOL_KIND_WEIGHT.get(chunk_kind, 0.4)
        file_boost = _file_relatedness_boost(context.file_path, target_paths)
        risk_boost = _risk_api_boost(context.content, source_identifiers, source_has_high_risk_api)
        identifier_boost = _identifier_overlap_boost(context.content, source_identifiers)
        distance_penalty = _call_distance_penalty(context.reason)
        noise_penalty = _noise_penalty(context.symbol_name, chunk_kind)
        score = (
            context.score
            + relation_boost
            + symbol_boost
            + file_boost
            + risk_boost
            + identifier_boost
            - distance_penalty
            - noise_penalty
        )
        rescored.append(
            RetrievedContext(
                chunk_id=context.chunk_id,
                evidence_id=context.evidence_id,
                file_path=context.file_path,
                symbol_name=context.symbol_name,
                start_line=context.start_line,
                end_line=context.end_line,
                content=context.content,
                reason=context.reason,
                score=score,
            )
        )
    return sorted(rescored, key=attrgetter("score"), reverse=True)


def _prune_ranked_contexts(contexts: list[RetrievedContext], *, limit: int, purpose: str = RAG_PURPOSE_DEFAULT) -> list[RetrievedContext]:
    if not contexts:
        return []
    definition_only = purpose in {RAG_PURPOSE_CANDIDATE, RAG_PURPOSE_CONFIRMATION}
    hard_limit = max(3, min(limit, 8 if definition_only else 6))
    bucket_caps = (
        {
            REASON_CALL: 2,
            REASON_SYMBOL: 3,
            REASON_INCLUDE: 1,
            REASON_MISSING: 3,
        }
        if definition_only
        else {
            REASON_CALL: 3,
            REASON_SYMBOL: 2,
            REASON_INCLUDE: 1,
            REASON_UPSTREAM: 2,
            REASON_KEYWORD: 2,
            REASON_VECTOR: 1,
            "qdrant": 2,
            REASON_MISSING: 4,
        }
    )
    selected: list[RetrievedContext] = []
    bucket_counts: dict[str, int] = {}
    symbol_counts: dict[tuple[str, str], int] = {}
    line_windows: set[tuple[str, int, str]] = set()

    def can_add(context: RetrievedContext, *, allow_same_region: bool = False) -> bool:
        bucket = _reason_bucket(context.reason)
        if context in selected:
            return False
        if any(_overlaps_same_symbol(context, item) for item in selected):
            return False
        if bucket_counts.get(bucket, 0) >= bucket_caps.get(bucket, 1):
            return False
        symbol_key = (context.file_path, context.symbol_name or "")
        if context.symbol_name and symbol_counts.get(symbol_key, 0) >= 1:
            return False
        line_window = (context.file_path, context.start_line // 20, bucket)
        if not allow_same_region and line_window in line_windows:
            return False
        return True

    def add(context: RetrievedContext) -> None:
        bucket = _reason_bucket(context.reason)
        selected.append(context)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if context.symbol_name:
            symbol_key = (context.file_path, context.symbol_name)
            symbol_counts[symbol_key] = symbol_counts.get(symbol_key, 0) + 1
        line_windows.add((context.file_path, context.start_line // 20, bucket))

    strong_buckets = {REASON_MISSING, REASON_CALL, REASON_SYMBOL}
    bucket_order = (
        (REASON_MISSING, REASON_CALL, REASON_SYMBOL, REASON_INCLUDE)
        if definition_only
        else (REASON_MISSING, REASON_CALL, REASON_SYMBOL, REASON_UPSTREAM, REASON_KEYWORD, "qdrant", REASON_INCLUDE, REASON_VECTOR)
    )
    for bucket in bucket_order:
        for context in contexts:
            if _reason_bucket(context.reason) != bucket:
                continue
            if not can_add(context):
                continue
            add(context)
            break
        if len(selected) >= hard_limit:
            return sorted(selected, key=attrgetter("score"), reverse=True)

    strong_symbols = {context.symbol_name for context in selected if context.symbol_name}
    top_score = contexts[0].score
    weak_score_floor = top_score - 2.2
    for context in contexts:
        if context in selected:
            continue
        bucket = _reason_bucket(context.reason)
        if bucket in strong_buckets:
            continue
        if definition_only and bucket not in bucket_caps:
            continue
        if bucket == REASON_INCLUDE and selected:
            continue
        if context.symbol_name and context.symbol_name in strong_symbols:
            continue
        if bucket == REASON_VECTOR and selected:
            continue
        if bucket in {REASON_KEYWORD, REASON_VECTOR} and context.score < weak_score_floor:
            continue
        if not can_add(context):
            continue
        add(context)
        if len(selected) >= hard_limit:
            break
    for context in contexts:
        if len(selected) >= hard_limit:
            break
        if not can_add(context, allow_same_region=True):
            continue
        add(context)
    return sorted(selected, key=attrgetter("score"), reverse=True)


def _overlaps_same_symbol(left: RetrievedContext, right: RetrievedContext) -> bool:
    if left.file_path != right.file_path or not left.symbol_name or left.symbol_name != right.symbol_name:
        return False
    return left.start_line <= right.end_line and right.start_line <= left.end_line


def _context_merge_key(context: RetrievedContext) -> tuple[int, float]:
    priority = {
        REASON_CALL: 6,
        REASON_SYMBOL: 5,
        REASON_INCLUDE: 4,
        REASON_UPSTREAM: 3,
        REASON_KEYWORD: 2,
        REASON_VECTOR: 1,
        "qdrant": 2,
        REASON_MISSING: 7,
    }.get(_reason_bucket(context.reason), 0)
    return priority, context.score


def _bucket_counts(contexts: list[RetrievedContext]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for context in contexts:
        bucket = _reason_bucket(context.reason)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _context_to_diagnostic(context: RetrievedContext) -> dict:
    return {
        "evidence_id": context.evidence_id,
        "file_path": context.file_path,
        "symbol_name": context.symbol_name,
        "start_line": context.start_line,
        "end_line": context.end_line,
        "reason": context.reason,
        "score": round(context.score, 4),
    }


def _definition_profile_enabled(settings: Settings) -> bool:
    return settings.rag_retrieval_profile.strip().lower() in {"definition", "definitions", "minimal"}


def _definition_only_mode(settings: Settings, purpose: str) -> bool:
    return purpose in {RAG_PURPOSE_CANDIDATE, RAG_PURPOSE_CONFIRMATION} or _definition_profile_enabled(settings)


def _definition_symbol_allowed(kind: str) -> bool:
    return kind in {
        "function",
        "declaration",
        "macro",
        "type",
        "typedef",
        "struct",
        "enum",
        "global_variable",
        "callback_binding",
        "function_pointer",
    }


def _definition_chunk_allowed(chunk: CodeChunk, target_paths: set[str]) -> bool:
    useful_kinds = {
        "macro",
        "conditional",
        "type",
        "struct",
        "typedef",
        "enum",
        "declaration",
        "function_pointer",
        "callback_binding",
        "global_variable",
    }
    if chunk.chunk_kind in useful_kinds:
        return True
    if chunk.chunk_kind == "function":
        return chunk.file.relative_path not in target_paths and _line_span(chunk) <= 40
    if chunk.chunk_kind == "callsite":
        return chunk.file.relative_path not in target_paths
    return False


def _filter_contexts_for_profile(
    db: Session,
    project: CodeProject,
    contexts: list[RetrievedContext],
    target_paths: set[str],
    settings: Settings,
    *,
    purpose: str = RAG_PURPOSE_DEFAULT,
) -> list[RetrievedContext]:
    if not _definition_only_mode(settings, purpose):
        return contexts
    chunks_by_id = _chunks_by_id(db, project)
    filtered: list[RetrievedContext] = []
    for context in contexts:
        chunk = chunks_by_id.get(context.chunk_id or "")
        if chunk is None:
            filtered.append(context)
            continue
        if _definition_chunk_allowed(chunk, target_paths):
            filtered.append(context)
    return filtered


def _line_span(chunk: CodeChunk) -> int:
    return max(0, chunk.end_line - chunk.start_line + 1)


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
            contexts.append(_context_from_chunk(chunk, f"{REASON_CALL}:d{edge_depth}", 2.5 + edge.confidence - (0.25 * (edge_depth - 1))))
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
            contexts.append(_context_from_chunk(chunk, REASON_INCLUDE, 1.8))
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
        _context_from_chunk(chunk, REASON_UPSTREAM, 1.8 + edge.confidence)
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
        _context_from_chunk(chunk, f"{REASON_SYMBOL}:{edge.edge_type.lower()}", 1.5 + edge.confidence)
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
        if chunk.file and chunk.file.relative_path in target_paths:
            continue
        contexts.append(_context_from_chunk(chunk, f"{REASON_KEYWORD}:{hit.reason}", 0.4 + hit.score))
    return sorted(contexts, key=attrgetter("score"), reverse=True)[:limit]


def _vector_contexts(
    db: Session,
    project: CodeProject,
    source_text: str,
    target_paths: set[str],
    *,
    limit: int,
    settings: Settings | None = None,
) -> list[RetrievedContext]:
    settings = settings or get_settings()
    query_vector = embed_text_with_settings(source_text, settings)
    source_identifiers = _identifiers(source_text)
    scored: list[tuple[float, CodeChunk]] = []
    for chunk in db.scalars(select(CodeChunk).where(CodeChunk.project_id == project.id)).all():
        if chunk.file.relative_path in target_paths and chunk.chunk_kind in {"function", "file_summary"}:
            continue
        if source_identifiers and not (_identifiers(chunk.content) & source_identifiers):
            continue
        score = _cosine_similarity(query_vector, embed_text_with_settings(chunk.content, settings))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_context_from_chunk(chunk, REASON_VECTOR, 0.5 + score) for score, chunk in scored[:limit]]


def _qdrant_contexts(
    db: Session,
    project: CodeProject,
    source_text: str,
    target_paths: set[str],
    *,
    settings: Settings,
    limit: int,
) -> list[RetrievedContext]:
    client = QdrantCodeIndexClient(settings)
    if not client.enabled:
        return []
    try:
        results = client.search_sync(
            embed_text_with_settings(source_text, settings),
            project_id=project.id,
            limit=limit,
        )
    except Exception:
        return []
    chunks_by_id = _chunks_by_id(db, project)
    source_identifiers = _identifiers(source_text)
    contexts: list[RetrievedContext] = []
    for item in results:
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        chunk_id = payload.get("chunk_id")
        chunk = chunks_by_id.get(str(chunk_id)) if chunk_id else None
        if chunk is None:
            continue
        if chunk.file.relative_path in target_paths and chunk.chunk_kind in {"function", "file_summary"}:
            continue
        if source_identifiers and not (_identifiers(chunk.content) & source_identifiers):
            continue
        score = float(item.get("score") or 0.0)
        contexts.append(_context_from_chunk(chunk, "qdrant", 0.7 + score))
    return sorted(contexts, key=attrgetter("score"), reverse=True)[:limit]


def _chunks_by_symbol(db: Session, project: CodeProject) -> dict[str, CodeChunk]:
    chunks = db.scalars(
        select(CodeChunk).where(CodeChunk.project_id == project.id, CodeChunk.symbol_id.is_not(None))
    ).all()
    by_symbol: dict[str, CodeChunk] = {}
    for chunk in chunks:
        if chunk.symbol_id and (chunk.symbol_id not in by_symbol or chunk.chunk_kind == "function"):
            by_symbol[chunk.symbol_id] = chunk
    return by_symbol


def _chunks_by_id(db: Session, project: CodeProject) -> dict[str, CodeChunk]:
    return {
        chunk.id: chunk
        for chunk in db.scalars(select(CodeChunk).where(CodeChunk.project_id == project.id)).all()
    }


def _relation_boost(reason: str) -> float:
    bucket = _reason_bucket(reason)
    if bucket == REASON_CALL:
        return 3.0
    if bucket == REASON_MISSING:
        return 3.4
    if bucket == REASON_SYMBOL:
        return 1.65
    if bucket == REASON_INCLUDE:
        return 0.95
    if bucket == REASON_UPSTREAM:
        return 0.9
    if bucket == REASON_KEYWORD:
        return 0.1
    if bucket == REASON_VECTOR:
        return 0.1
    if bucket == "qdrant":
        return 0.25
    return 0.0


def _kind_from_reason(reason: str) -> str:
    bucket = _reason_bucket(reason)
    if bucket == REASON_INCLUDE:
        return "file_summary"
    if bucket == REASON_CALL:
        return "function"
    if bucket in {REASON_VECTOR, "qdrant", REASON_MISSING}:
        return "function"
    return "symbol"


def _reason_bucket(reason: str) -> str:
    return reason.split(":", 1)[0]


def _file_relatedness_boost(file_path: str, target_paths: set[str]) -> float:
    return max((_path_relatedness(file_path, target_path) for target_path in target_paths), default=0.0)


def _path_relatedness(left: str, right: str) -> float:
    left_parts = [part for part in left.replace("\\", "/").split("/") if part]
    right_parts = [part for part in right.replace("\\", "/").split("/") if part]
    if not left_parts or not right_parts:
        return 0.0
    common_prefix = 0
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part != right_part:
            break
        common_prefix += 1
    left_stem = left_parts[-1].rsplit(".", 1)[0]
    right_stem = right_parts[-1].rsplit(".", 1)[0]
    stem_bonus = 1.0 if left_stem == right_stem else 0.0
    same_parent_bonus = 0.7 if len(left_parts) > 1 and len(right_parts) > 1 and left_parts[-2] == right_parts[-2] else 0.0
    return min(2.0, (common_prefix * 0.35) + stem_bonus + same_parent_bonus)


def _risk_api_boost(content: str, source_identifiers: set[str], source_has_high_risk_api: bool) -> float:
    identifiers = _identifiers(content)
    risk_hits = identifiers & _HIGH_RISK_IDENTIFIERS
    source_overlap = identifiers & source_identifiers & _HIGH_RISK_IDENTIFIERS
    return (0.35 * len(risk_hits)) + (0.55 * len(source_overlap)) + (0.25 if source_has_high_risk_api and risk_hits else 0.0)


def _identifier_overlap_boost(content: str, source_identifiers: set[str]) -> float:
    if not source_identifiers:
        return 0.0
    overlap = _identifiers(content) & source_identifiers
    return min(1.2, 0.12 * len(overlap))


def _call_distance_penalty(reason: str) -> float:
    match = re.search(r":d(\d+)", reason)
    if not match:
        return 0.0
    return max(0, int(match.group(1)) - 1) * 0.45


def _noise_penalty(symbol_name: str | None, chunk_kind: str) -> float:
    if not symbol_name:
        return 0.0
    if chunk_kind in {"global_variable", "macro"} and (len(symbol_name) <= 4 or symbol_name.startswith("_")):
        return 0.8
    if symbol_name in {"err", "ret", "tmp", "value", "parent", "start", "config", "callback"}:
        return 1.2
    return 0.0


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
        if symbol.kind in {"function", "declaration"}
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


def _referenced_symbols(source_text: str) -> set[str]:
    calls = {
        match.group(1)
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", source_text)
        if match.group(1) not in _COMMON_IDENTIFIERS
    }
    macros = {name for name in _identifiers(source_text) if name.isupper() and len(name) > 2}
    typed_names = {
        match.group(1)
        for match in re.finditer(r"\b(?:struct|enum|union)\s+([A-Za-z_][A-Za-z0-9_]*)", source_text)
    }
    casts = {
        match.group(1)
        for match in re.finditer(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\*+\s*)?\)", source_text)
        if not _is_low_value_rag_identifier(match.group(1))
    }
    field_names = {
        match.group(1)
        for match in re.finditer(r"(?:->|\.)\s*([A-Za-z_][A-Za-z0-9_]*)", source_text)
    }
    referenced = (calls | macros | typed_names | casts) - field_names
    return {
        name
        for name in referenced
        if len(name) > 2 and not _is_low_value_rag_identifier(name)
    }


def _locally_defined_symbols(source_text: str) -> set[str]:
    defined: set[str] = set()
    for pattern in (
        r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\b(?:static\s+)?(?:inline\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{",
        r"\btypedef\b.*?\b([A-Za-z_][A-Za-z0-9_]*)\s*;",
        r"\b(?:struct|enum|union)\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:extern\s+)?(?:static\s+)?(?:const\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)",
        r"^\s*(?:const\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;|,)",
    ):
        defined.update(match.group(1) for match in re.finditer(pattern, source_text, flags=re.MULTILINE | re.DOTALL))
    defined.update(_function_parameter_symbols(source_text))
    defined.update(_struct_field_symbols(source_text))
    defined.update(_enum_member_symbols(source_text))
    return defined


def _function_parameter_symbols(source_text: str) -> set[str]:
    params: set[str] = set()
    for match in re.finditer(
        r"\b[A-Za-z_][A-Za-z0-9_\s\*]*\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^;{}]*)\)\s*\{",
        source_text,
        flags=re.MULTILINE,
    ):
        for raw_param in match.group(1).split(","):
            param = raw_param.strip()
            if not param or param == "void":
                continue
            name_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?$", param)
            if name_match:
                params.add(name_match.group(1))
    return params


def _struct_field_symbols(source_text: str) -> set[str]:
    fields: set[str] = set()
    for body in re.findall(r"\bstruct\b[^{;]*\{(.*?)\}", source_text, flags=re.DOTALL):
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*;", body):
            fields.add(match.group(1))
    return fields


def _enum_member_symbols(source_text: str) -> set[str]:
    members: set[str] = set()
    for body in re.findall(r"\benum\b[^{;]*\{(.*?)\}", source_text, flags=re.DOTALL):
        for part in body.split(","):
            match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", part.strip())
            if match:
                members.add(match.group(1))
    return members


def _missing_symbol_score(kind: str, name: str, file_path: str, target_paths: set[str]) -> float:
    kind_weight = {
        "function": 5.2,
        "declaration": 4.9,
        "macro": 4.6,
        "type": 4.3,
        "typedef": 4.3,
        "struct": 4.1,
        "enum": 4.0,
        "global_variable": 3.9,
        "callback_binding": 3.5,
    }.get(kind, 3.0)
    name_weight = 0.4 if name.isupper() else 0.0
    return kind_weight + name_weight + _file_relatedness_boost(file_path, target_paths)


def _identifiers(source_text: str) -> set[str]:
    return {
        match.group(0)
        for match in _IDENTIFIER_RE.finditer(source_text)
        if match.group(0) not in _COMMON_IDENTIFIERS and len(match.group(0)) > 2
    }


def _rag_query_identifiers(source_text: str) -> set[str]:
    identifiers = _identifiers(source_text)
    return {name for name in identifiers if not _is_low_value_rag_identifier(name)}


def _is_low_value_rag_identifier(name: str) -> bool:
    if name in _LOW_VALUE_RAG_IDENTIFIERS:
        return True
    if name.startswith("IS_"):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name) and any(token in name for token in ("_", "FLAG", "IT", "MODE", "STATE")):
        return True
    if re.fullmatch(r"[us]int(?:8|16|32|64)_t", name):
        return True
    return False


def _has_high_risk_api(source_text: str) -> bool:
    return bool(
        re.search(
            r"\b(memcpy|memmove|strcpy|strncpy|sprintf|malloc|calloc|realloc|free|open|close|fopen|fclose|mutex_lock|mutex_unlock)\s*\(",
            source_text,
        )
    )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))

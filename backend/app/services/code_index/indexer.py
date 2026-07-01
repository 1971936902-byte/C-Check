from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.chunker import build_chunks_for_file
from app.services.code_index.clangd import probe_clangd
from app.services.code_index.embeddings import sync_project_embeddings, upsert_project_embeddings_to_qdrant_sync
from app.services.code_index.graph_builder import build_include_edges, build_symbol_edges, build_usage_edges
from app.services.code_index.parse_cache import (
    load_cached_chunk_templates,
    load_cached_parse,
    store_cached_chunk_templates,
    store_cached_parse,
)
from app.services.code_index.parser import PARSER_VERSION, parse_c_source
from app.services.code_index.tree_sitter_c import probe_tree_sitter_c


def build_code_index(
    db: Session,
    task: ReviewTask,
    *,
    settings: Settings | None = None,
) -> CodeProject:
    settings = settings or get_settings()
    source_hash = _task_source_hash(task.files)
    embedding_signature = _embedding_signature(settings)
    existing = task.code_project
    if (
        existing
        and existing.source_hash == source_hash
        and existing.parser_version == PARSER_VERSION
        and existing.embedding_backend == embedding_signature
    ):
        return existing
    if existing:
        db.delete(existing)
        db.flush()

    project = CodeProject(
        task=task,
        source_hash=source_hash,
        parser_version=PARSER_VERSION,
        embedding_backend=embedding_signature,
        stats_json={},
    )
    db.add(project)
    db.flush()

    parsed_by_file: dict[str, tuple[ReviewFile, CodeFile, object, list[CodeSymbol]]] = {}
    symbol_by_file_and_name: dict[tuple[str, str], CodeSymbol] = {}
    symbols_by_name: dict[str, list[CodeSymbol]] = defaultdict(list)
    symbol_count = 0
    chunk_count = 0
    edge_count = 0
    parse_cache_hits = 0
    parse_cache_misses = 0
    chunk_cache_hits = 0
    chunk_cache_misses = 0

    for review_file in task.files:
        file_content_hash = _hash_text(review_file.source_text)
        parsed = load_cached_parse(
            db,
            relative_path=review_file.relative_path,
            content_hash=file_content_hash,
            settings=settings,
        )
        if parsed is None:
            parsed = parse_c_source(review_file.relative_path, review_file.source_text)
            store_cached_parse(
                db,
                relative_path=review_file.relative_path,
                content_hash=file_content_hash,
                parsed=parsed,
                settings=settings,
            )
            parse_cache_misses += 1
        else:
            parse_cache_hits += 1
        code_file = CodeFile(
            project=project,
            review_file_id=review_file.id,
            relative_path=review_file.relative_path,
            language=_language_for_path(review_file.relative_path),
            size_bytes=review_file.size_bytes,
            content_hash=file_content_hash,
            line_count=parsed.line_count,
        )
        db.add(code_file)
        db.flush()
        file_symbols: list[CodeSymbol] = []
        for parsed_symbol in parsed.symbols:
            symbol = CodeSymbol(
                project=project,
                file=code_file,
                kind=parsed_symbol.kind,
                name=parsed_symbol.name,
                signature=parsed_symbol.signature,
                scope="global",
                start_line=parsed_symbol.start_line,
                end_line=parsed_symbol.end_line,
                confidence=parsed_symbol.confidence,
                source_tool=parsed_symbol.source_tool,
            )
            db.add(symbol)
            db.flush()
            file_symbols.append(symbol)
            symbol_count += 1
            symbol_by_file_and_name[(review_file.relative_path, parsed_symbol.name)] = symbol
            symbols_by_name[parsed_symbol.name].append(symbol)
        parsed_by_file[review_file.relative_path] = (review_file, code_file, parsed, file_symbols)
        cached_chunks = load_cached_chunk_templates(db, content_hash=file_content_hash, settings=settings)
        if cached_chunks is None:
            chunks = build_chunks_for_file(project, code_file, review_file, parsed, file_symbols)
            store_cached_chunk_templates(
                db,
                content_hash=file_content_hash,
                settings=settings,
                chunks=[_chunk_template(chunk) for chunk in chunks],
            )
            chunk_cache_misses += 1
        else:
            chunks = _chunks_from_templates(project, code_file, file_symbols, cached_chunks)
            chunk_cache_hits += 1
        for chunk in chunks:
            db.add(chunk)
            chunk_count += 1

    db.flush()
    for review_file, code_file, parsed, _file_symbols in parsed_by_file.values():
        for edge in build_include_edges(project, code_file, parsed):
            db.add(edge)
            edge_count += 1
        for edge in build_symbol_edges(project, code_file, parsed, _file_symbols, symbols_by_name):
            db.add(edge)
            edge_count += 1

    db.flush()
    for _review_file, _code_file, _parsed, file_symbols in parsed_by_file.values():
        for edge in build_usage_edges(project, file_symbols, symbols_by_name):
            db.add(edge)
            edge_count += 1

    qdrant_points = 0
    qdrant_error = None
    embedding_cache_stats: dict[str, int] = {}
    if settings.rag_qdrant_url:
        try:
            qdrant_points = upsert_project_embeddings_to_qdrant_sync(
                db, project, settings=settings, cache_stats=embedding_cache_stats
            )
        except Exception as exc:
            if not settings.rag_embedding_allow_hash_fallback:
                raise
            qdrant_error = str(exc)
    else:
        sync_project_embeddings(db, project, settings=settings, cache_stats=embedding_cache_stats)
    stats_json = {
        "files": len(task.files),
        "symbols": symbol_count,
        "chunks": chunk_count,
        "edges": edge_count,
        "embeddings": len(project.chunks),
        "qdrant_points": qdrant_points,
        "embedding_backend": settings.rag_embedding_backend,
        "embedding_model": settings.rag_embedding_model,
        "embedding_dimension": settings.rag_embedding_dimension,
        "parse_cache_hits": parse_cache_hits,
        "parse_cache_misses": parse_cache_misses,
        "chunk_cache_hits": chunk_cache_hits,
        "chunk_cache_misses": chunk_cache_misses,
        "embedding_cache_hits": embedding_cache_stats.get("hits", 0),
        "embedding_cache_misses": embedding_cache_stats.get("misses", 0),
        "parser": PARSER_VERSION,
        "tree_sitter": probe_tree_sitter_c().__dict__,
        "clang": probe_clangd().__dict__,
    }
    if qdrant_error:
        stats_json["qdrant_error"] = qdrant_error
    project.stats_json = stats_json
    db.flush()
    return project


def load_or_build_code_index(db: Session, task: ReviewTask, *, settings: Settings | None = None) -> CodeProject:
    project = db.scalar(select(CodeProject).where(CodeProject.task_id == task.id))
    if project:
        task.code_project = project
    return build_code_index(db, task, settings=settings)


def _task_source_hash(files: list[ReviewFile]) -> str:
    digest = hashlib.sha256()
    for source in sorted(files, key=lambda file: file.relative_path):
        digest.update(source.relative_path.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
        digest.update(source.source_text.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def _embedding_signature(settings: Settings) -> str:
    payload = "|".join(
        [
            settings.rag_embedding_backend,
            settings.rag_embedding_model,
            str(settings.rag_embedding_dimension),
            settings.rag_qdrant_collection,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _language_for_path(path: str) -> str:
    return "c_header" if path.lower().endswith(".h") else "c"


def _chunk_template(chunk: CodeChunk) -> dict:
    return {
        "chunk_kind": chunk.chunk_kind,
        "symbol_name": chunk.symbol_name,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "content": chunk.content,
        "content_hash": chunk.content_hash,
        "token_estimate": chunk.token_estimate,
        "metadata_json": chunk.metadata_json or {},
    }


def _chunks_from_templates(
    project: CodeProject,
    code_file: CodeFile,
    file_symbols: list[CodeSymbol],
    templates: list[dict],
) -> list[CodeChunk]:
    symbols_by_name = {symbol.name: symbol for symbol in file_symbols}
    return [
        CodeChunk(
            project=project,
            file=code_file,
            symbol=symbols_by_name.get(str(item.get("symbol_name") or "")),
            chunk_kind=str(item.get("chunk_kind") or "window"),
            symbol_name=item.get("symbol_name") if isinstance(item.get("symbol_name"), str) else None,
            start_line=int(item.get("start_line") or 1),
            end_line=int(item.get("end_line") or item.get("start_line") or 1),
            content=str(item.get("content") or ""),
            content_hash=str(item.get("content_hash") or _hash_text(str(item.get("content") or ""))),
            token_estimate=int(item.get("token_estimate") or 0),
            metadata_json=dict(item.get("metadata_json") or {}),
        )
        for item in templates
    ]

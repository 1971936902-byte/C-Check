from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.chunker import build_chunks_for_file
from app.services.code_index.embeddings import sync_project_embeddings, upsert_project_embeddings_to_qdrant_sync
from app.services.code_index.graph_builder import build_include_edges, build_symbol_edges, build_usage_edges
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
    existing = task.code_project
    if existing and existing.source_hash == source_hash and existing.parser_version == PARSER_VERSION:
        return existing
    if existing:
        db.delete(existing)
        db.flush()

    project = CodeProject(
        task=task,
        source_hash=source_hash,
        parser_version=PARSER_VERSION,
        embedding_backend="qdrant" if settings.rag_qdrant_url else None,
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

    for review_file in task.files:
        parsed = parse_c_source(review_file.relative_path, review_file.source_text)
        code_file = CodeFile(
            project=project,
            review_file_id=review_file.id,
            relative_path=review_file.relative_path,
            language=_language_for_path(review_file.relative_path),
            size_bytes=review_file.size_bytes,
            content_hash=_hash_text(review_file.source_text),
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
        for chunk in build_chunks_for_file(project, code_file, review_file, parsed, file_symbols):
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

    sync_project_embeddings(db, project, settings=settings)
    qdrant_points = 0
    qdrant_error = None
    if settings.rag_qdrant_url:
        try:
            qdrant_points = upsert_project_embeddings_to_qdrant_sync(db, project, settings=settings)
        except Exception as exc:
            qdrant_error = str(exc)
    stats_json = {
        "files": len(task.files),
        "symbols": symbol_count,
        "chunks": chunk_count,
        "edges": edge_count,
        "embeddings": len(project.chunks),
        "qdrant_points": qdrant_points,
        "parser": PARSER_VERSION,
        "tree_sitter": probe_tree_sitter_c().__dict__,
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


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _language_for_path(path: str) -> str:
    return "c_header" if path.lower().endswith(".h") else "c"

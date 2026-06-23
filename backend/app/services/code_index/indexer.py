from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeEdge, CodeFile, CodeProject, CodeSymbol, ReviewFile, ReviewTask
from app.services.code_index.chunker import build_chunks_for_file
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
        for include in parsed.includes:
            db.add(
                CodeEdge(
                    project=project,
                    source_id=code_file.id,
                    target_id=None,
                    edge_type="FILE_INCLUDES_FILE",
                    line=include.line,
                    confidence=0.7,
                    source_tool=PARSER_VERSION,
                    metadata_json={"include": include.target},
                )
            )
            edge_count += 1
        for parsed_symbol in parsed.symbols:
            symbol = symbol_by_file_and_name.get((review_file.relative_path, parsed_symbol.name))
            if not symbol:
                continue
            db.add(
                CodeEdge(
                    project=project,
                    source_id=code_file.id,
                    target_id=symbol.id,
                    edge_type="FILE_CONTAINS_SYMBOL",
                    line=parsed_symbol.start_line,
                    confidence=parsed_symbol.confidence,
                    source_tool=PARSER_VERSION,
                    metadata_json={"symbol_name": parsed_symbol.name, "symbol_kind": parsed_symbol.kind},
                )
            )
            edge_count += 1
        for call in parsed.calls:
            source_symbol = symbol_by_file_and_name.get((review_file.relative_path, call.caller_name))
            if not source_symbol:
                continue
            target_symbol = _best_symbol_match(symbols_by_name.get(call.callee_name, []), review_file.relative_path)
            db.add(
                CodeEdge(
                    project=project,
                    source_id=source_symbol.id,
                    target_id=target_symbol.id if target_symbol else None,
                    edge_type="FUNCTION_CALLS_FUNCTION",
                    line=call.line,
                    confidence=0.85 if target_symbol else 0.45,
                    source_tool=PARSER_VERSION,
                    metadata_json={"callee_name": call.callee_name},
                )
            )
            edge_count += 1

    project.stats_json = {
        "files": len(task.files),
        "symbols": symbol_count,
        "chunks": chunk_count,
        "edges": edge_count,
        "parser": PARSER_VERSION,
        "tree_sitter": probe_tree_sitter_c().__dict__,
    }
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


def _best_symbol_match(symbols: list[CodeSymbol], caller_path: str) -> CodeSymbol | None:
    if not symbols:
        return None
    for symbol in symbols:
        if symbol.file and symbol.file.relative_path == caller_path:
            return symbol
    functions = [symbol for symbol in symbols if symbol.kind == "function"]
    return functions[0] if functions else symbols[0]

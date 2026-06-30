from __future__ import annotations

import hashlib

from app.db.models import CodeChunk, CodeFile, CodeProject, CodeSymbol, ReviewFile
from app.services.code_index.parser import ParsedFile


LARGE_DATA_CHUNK_MAX_CHARS = 8_000


def build_chunks_for_file(
    project: CodeProject,
    code_file: CodeFile,
    review_file: ReviewFile,
    parsed: ParsedFile,
    symbols: list[CodeSymbol],
) -> list[CodeChunk]:
    chunks: list[CodeChunk] = [_file_summary_chunk(project, code_file, review_file, parsed)]
    symbol_by_name = {symbol.name: symbol for symbol in symbols}
    for symbol in symbols:
        if symbol.kind in {
            "function",
            "macro",
            "conditional",
            "type",
            "struct",
            "union",
            "typedef",
            "enum",
            "declaration",
            "global_variable",
            "function_pointer",
            "callback_binding",
        }:
            chunks.append(_symbol_chunk(project, code_file, symbol, review_file.source_text, parsed))
            if symbol.kind == "function" and symbol.end_line - symbol.start_line > 80:
                chunks.extend(_sliding_window_chunks(project, code_file, symbol, review_file.source_text))
    for call in parsed.calls:
        caller = symbol_by_name.get(call.caller_name)
        chunks.append(_callsite_chunk(project, code_file, caller, review_file.source_text, call.callee_name, call.line))
    return chunks


def _file_summary_chunk(project: CodeProject, code_file: CodeFile, source: ReviewFile, parsed: ParsedFile) -> CodeChunk:
    functions = [symbol.name for symbol in parsed.symbols if symbol.kind == "function"][:30]
    declarations = [
        symbol.name
        for symbol in parsed.symbols
        if symbol.kind in {"declaration", "macro", "conditional", "type", "struct", "union", "typedef", "enum", "function_pointer"}
    ][:30]
    includes = [include.target for include in parsed.includes][:30]
    content = "\n".join(
        [
            f"File: {source.relative_path}",
            f"Includes: {', '.join(includes) if includes else 'none'}",
            f"Functions: {', '.join(functions) if functions else 'none'}",
            f"Declarations/macros/types: {', '.join(declarations) if declarations else 'none'}",
        ]
    )
    return _chunk(
        project,
        code_file,
        None,
        "file_summary",
        None,
        1,
        max(1, parsed.line_count),
        content,
        {"includes": includes, "functions": functions, "declarations": declarations},
    )


def _symbol_chunk(
    project: CodeProject,
    code_file: CodeFile,
    symbol: CodeSymbol,
    source_text: str,
    parsed: ParsedFile,
) -> CodeChunk:
    lines = source_text.splitlines()
    chunk_end_line = symbol.start_line if symbol.kind == "conditional" else symbol.end_line
    content = "\n".join(lines[symbol.start_line - 1 : chunk_end_line])
    if not content:
        content = symbol.signature or symbol.name
    called_symbols = sorted({call.callee_name for call in parsed.calls if call.caller_name == symbol.name})
    used_macros = sorted(
        symbol_name.name
        for symbol_name in parsed.symbols
        if symbol_name.kind == "macro" and symbol_name.name in content and symbol_name.name != symbol.name
    )
    used_types = sorted(
        symbol_name.name
        for symbol_name in parsed.symbols
        if symbol_name.kind in {"type", "struct", "union", "typedef", "enum"} and symbol_name.name in content and symbol_name.name != symbol.name
    )
    used_globals = sorted(
        symbol_name.name
        for symbol_name in parsed.symbols
        if symbol_name.kind == "global_variable" and symbol_name.name in content and symbol_name.name != symbol.name
    )
    used_callbacks = sorted(
        symbol_name.name
        for symbol_name in parsed.symbols
        if symbol_name.kind == "function_pointer" and symbol_name.name in content and symbol_name.name != symbol.name
    )
    used_conditionals = sorted(
        symbol_name.name
        for symbol_name in parsed.symbols
        if symbol_name.kind == "conditional" and symbol_name.name.split("_", 1)[-1] in content and symbol_name.name != symbol.name
    )
    large_data_metadata: dict = {}
    if symbol.kind == "global_variable" and _looks_like_large_data_initializer(content):
        content, large_data_metadata = _summarize_large_data_initializer(
            symbol.name,
            content,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
        )
    return _chunk(
        project,
        code_file,
        symbol,
        symbol.kind,
        symbol.name,
        symbol.start_line,
        chunk_end_line,
        content,
        {
            "symbol_kind": symbol.kind,
            "called_symbols": called_symbols,
            "used_macros": used_macros,
            "used_types": used_types,
            "used_globals": used_globals,
            "used_callbacks": used_callbacks,
            "used_conditionals": used_conditionals,
            "scope_end_line": symbol.end_line if symbol.kind == "conditional" else None,
            "source_tool": symbol.source_tool,
            **large_data_metadata,
        },
    )


def _looks_like_large_data_initializer(content: str) -> bool:
    if len(content) <= LARGE_DATA_CHUNK_MAX_CHARS or "=" not in content:
        return False
    declaration = content.split("=", 1)[0]
    return "[" in declaration or "{" in content


def _summarize_large_data_initializer(
    symbol_name: str,
    content: str,
    *,
    start_line: int,
    end_line: int,
) -> tuple[str, dict]:
    declaration = " ".join(content.split("=", 1)[0].split())
    declaration = f"{declaration} = <large initializer omitted>;"
    initializer = content.split("=", 1)[1]
    approximate_items = initializer.count(",") + 1 if initializer.strip() else 0
    content_digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    summary = "\n".join(
        [
            declaration,
            (
                "/* RAG large-data summary: "
                f"symbol={symbol_name}; lines={start_line}-{end_line}; "
                f"source_chars={len(content)}; approximate_items={approximate_items}; "
                f"sha256={content_digest}. */"
            ),
        ]
    )
    return summary, {
        "large_data_summary": True,
        "source_chars": len(content),
        "source_lines": max(1, end_line - start_line + 1),
        "approximate_items": approximate_items,
        "source_content_hash": content_digest,
    }


def _sliding_window_chunks(
    project: CodeProject,
    code_file: CodeFile,
    symbol: CodeSymbol,
    source_text: str,
    *,
    window_lines: int = 60,
    overlap_lines: int = 10,
) -> list[CodeChunk]:
    lines = source_text.splitlines()
    chunks: list[CodeChunk] = []
    cursor = symbol.start_line
    while cursor <= symbol.end_line:
        end = min(symbol.end_line, cursor + window_lines - 1)
        content_lines = [symbol.signature or symbol.name]
        content_lines.extend(lines[cursor - 1 : end])
        content = "\n".join(content_lines)
        chunks.append(
            _chunk(
                project,
                code_file,
                symbol,
                "function_window",
                symbol.name,
                cursor,
                end,
                content,
                {
                    "symbol_kind": symbol.kind,
                    "window_of": symbol.name,
                    "source_tool": symbol.source_tool,
                },
            )
        )
        if end >= symbol.end_line:
            break
        cursor = max(cursor + 1, end - overlap_lines + 1)
    return chunks


def _callsite_chunk(
    project: CodeProject,
    code_file: CodeFile,
    caller: CodeSymbol | None,
    source_text: str,
    callee_name: str,
    line: int,
) -> CodeChunk:
    lines = source_text.splitlines()
    start = max(1, line - 5)
    end = min(len(lines), line + 5)
    content = "\n".join(f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1))
    return _chunk(
        project,
        code_file,
        caller,
        "callsite",
        caller.name if caller else None,
        start,
        end,
        content,
        {"callee_name": callee_name, "caller_name": caller.name if caller else None},
    )


def _chunk(
    project: CodeProject,
    code_file: CodeFile,
    symbol: CodeSymbol | None,
    chunk_kind: str,
    symbol_name: str | None,
    start_line: int,
    end_line: int,
    content: str,
    metadata: dict,
) -> CodeChunk:
    return CodeChunk(
        project=project,
        file=code_file,
        symbol=symbol,
        chunk_kind=chunk_kind,
        symbol_name=symbol_name,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest(),
        token_estimate=max(1, len(content) // 4),
        metadata_json=metadata,
    )

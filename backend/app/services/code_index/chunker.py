from __future__ import annotations

import hashlib

from app.db.models import CodeChunk, CodeFile, CodeProject, CodeSymbol, ReviewFile
from app.services.code_index.parser import ParsedFile


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
        if symbol.kind in {"function", "macro", "type", "struct", "typedef", "enum", "declaration", "global_variable"}:
            chunks.append(_symbol_chunk(project, code_file, symbol, review_file.source_text, parsed))
    for call in parsed.calls:
        caller = symbol_by_name.get(call.caller_name)
        chunks.append(_callsite_chunk(project, code_file, caller, review_file.source_text, call.callee_name, call.line))
    return chunks


def _file_summary_chunk(project: CodeProject, code_file: CodeFile, source: ReviewFile, parsed: ParsedFile) -> CodeChunk:
    functions = [symbol.name for symbol in parsed.symbols if symbol.kind == "function"][:30]
    declarations = [symbol.name for symbol in parsed.symbols if symbol.kind in {"declaration", "macro", "type", "struct", "typedef", "enum"}][:30]
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
    content = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
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
        if symbol_name.kind in {"type", "struct", "typedef", "enum"} and symbol_name.name in content and symbol_name.name != symbol.name
    )
    return _chunk(
        project,
        code_file,
        symbol,
        symbol.kind,
        symbol.name,
        symbol.start_line,
        symbol.end_line,
        content,
        {
            "symbol_kind": symbol.kind,
            "called_symbols": called_symbols,
            "used_macros": used_macros,
            "used_types": used_types,
            "source_tool": symbol.source_tool,
        },
    )


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

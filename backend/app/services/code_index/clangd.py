from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ClangdStatus:
    available: bool
    executable: str | None = None
    libclang_available: bool = False
    libclang_reason: str | None = None
    reason: str | None = None


def probe_clangd() -> ClangdStatus:
    executable = shutil.which("clangd")
    libclang_available = True
    libclang_reason = None
    try:
        from clang import cindex  # noqa: F401
    except Exception as exc:
        libclang_available = False
        libclang_reason = f"python clang unavailable: {exc}"
    if executable is None:
        return ClangdStatus(
            available=False,
            executable=None,
            libclang_available=libclang_available,
            libclang_reason=libclang_reason,
            reason="clangd executable not found",
        )
    return ClangdStatus(
        available=True,
        executable=executable,
        libclang_available=libclang_available,
        libclang_reason=libclang_reason,
    )


@dataclass(frozen=True)
class LibclangParseResult:
    symbols: list[object] = field(default_factory=list)
    calls: list[object] = field(default_factory=list)


def parse_with_libclang(relative_path: str, source_text: str):
    """Best-effort semantic extraction with libclang.

    This is optional by design. If python-clang or libclang is unavailable, the
    normal tree-sitter/ctags/builtin parser remains the source of truth.
    """
    try:
        from clang import cindex

        from app.services.code_index.parser import ParsedCall, ParsedFile, ParsedInclude, ParsedSymbol
    except Exception:
        return None

    filename = relative_path.replace("\\", "/")
    args = [
        "-x",
        "c",
        "-std=c11",
        "-D__attribute__(x)=",
        "-D__declspec(x)=",
    ]
    args.extend(_compile_command_args(relative_path))
    try:
        index = cindex.Index.create()
        translation_unit = index.parse(
            filename,
            args=args,
            unsaved_files=[(filename, source_text)],
            options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except Exception:
        return None

    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []
    includes: list[ParsedInclude] = []
    seen_symbols: set[tuple[str, str, int]] = set()
    source_lines = source_text.splitlines()

    def is_current_file(cursor) -> bool:
        cursor_file = getattr(cursor.location, "file", None)
        if cursor_file is None:
            return False
        return str(cursor_file).replace("\\", "/").endswith(filename)

    def add_symbol(kind: str, cursor, confidence: float) -> None:
        if not cursor.spelling or not is_current_file(cursor):
            return
        if kind in {"struct", "union", "enum"} and cursor.spelling.startswith("__anon"):
            return
        start_line = max(1, int(cursor.extent.start.line or cursor.location.line or 1))
        end_line = max(start_line, int(cursor.extent.end.line or start_line))
        key = (kind, cursor.spelling, start_line)
        if key in seen_symbols:
            return
        seen_symbols.add(key)
        signature = _signature_for_lines(source_lines, start_line, end_line, cursor.spelling)
        symbols.append(
            ParsedSymbol(
                kind=kind,
                name=cursor.spelling,
                signature=signature,
                start_line=start_line,
                end_line=end_line,
                confidence=confidence,
                source_tool="libclang",
            )
        )

    def is_real_type_declaration(cursor, keyword: str) -> bool:
        start_line = max(1, int(cursor.extent.start.line or cursor.location.line or 1))
        end_line = max(start_line, int(cursor.extent.end.line or start_line))
        source = "\n".join(source_lines[start_line - 1 : min(end_line, len(source_lines))]).strip()
        if "{" in source:
            return True
        name = cursor.spelling.strip()
        if not name:
            return False
        return bool(re.fullmatch(rf"{keyword}\s+{re.escape(name)}\s*;", source))

    def walk(cursor, current_function: str | None = None) -> None:
        kind = cursor.kind
        if kind == cindex.CursorKind.INCLUSION_DIRECTIVE and is_current_file(cursor):
            include_name = cursor.spelling
            if include_name:
                includes.append(ParsedInclude(target=include_name, line=max(1, cursor.location.line)))
        elif kind == cindex.CursorKind.FUNCTION_DECL and is_current_file(cursor):
            function_name = cursor.spelling
            if cursor.is_definition():
                add_symbol("function", cursor, 0.96)
                current_function = function_name or current_function
            else:
                add_symbol("declaration", cursor, 0.9)
        elif (
            kind == cindex.CursorKind.STRUCT_DECL
            and is_current_file(cursor)
            and is_real_type_declaration(cursor, "struct")
        ):
            add_symbol("struct", cursor, 0.88)
        elif (
            kind == cindex.CursorKind.UNION_DECL
            and is_current_file(cursor)
            and is_real_type_declaration(cursor, "union")
        ):
            add_symbol("union", cursor, 0.88)
        elif (
            kind == cindex.CursorKind.ENUM_DECL
            and is_current_file(cursor)
            and is_real_type_declaration(cursor, "enum")
        ):
            add_symbol("enum", cursor, 0.88)
        elif kind == cindex.CursorKind.TYPEDEF_DECL and is_current_file(cursor):
            add_symbol("typedef", cursor, 0.9)
        elif kind == cindex.CursorKind.VAR_DECL and is_current_file(cursor) and cursor.semantic_parent.kind == cindex.CursorKind.TRANSLATION_UNIT:
            add_symbol("global_variable", cursor, 0.82)
        elif kind == cindex.CursorKind.CALL_EXPR and current_function and is_current_file(cursor):
            callee_name = cursor.spelling or cursor.displayname
            if callee_name:
                calls.append(
                    ParsedCall(caller_name=current_function, callee_name=callee_name, line=max(1, cursor.location.line))
                )
        for child in cursor.get_children():
            walk(child, current_function)

    try:
        walk(translation_unit.cursor)
    except Exception:
        return None

    if not symbols and not calls and not includes:
        return None
    return ParsedFile(
        relative_path=relative_path,
        line_count=max(1, len(source_lines)),
        includes=includes,
        symbols=symbols,
        calls=calls,
    )


def _signature_for_lines(lines: list[str], start_line: int, end_line: int, fallback: str) -> str:
    if not lines:
        return fallback
    snippet = " ".join(line.strip() for line in lines[start_line - 1 : min(end_line, len(lines))])
    if "{" in snippet:
        snippet = snippet.split("{", 1)[0].strip()
    if ";" in snippet:
        snippet = snippet.split(";", 1)[0].strip() + ";"
    return snippet or fallback


def _compile_command_args(relative_path: str) -> list[str]:
    compile_commands = _find_compile_commands(Path.cwd())
    if compile_commands is None:
        return []
    try:
        entries = json.loads(compile_commands.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    wanted = relative_path.replace("\\", "/")
    best_entry = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        file_name = str(entry.get("file") or "").replace("\\", "/")
        if file_name.endswith(wanted) or wanted.endswith(file_name):
            best_entry = entry
            break
    if best_entry is None:
        return []
    raw_args = best_entry.get("arguments")
    if isinstance(raw_args, list):
        tokens = [str(item) for item in raw_args]
    else:
        command = str(best_entry.get("command") or "")
        tokens = shlex.split(command, posix=False) if command else []
    return _filter_compile_args(tokens)


def _find_compile_commands(start: Path) -> Path | None:
    for path in [start, *start.parents]:
        candidate = path / "compile_commands.json"
        if candidate.exists():
            return candidate
    return None


def _filter_compile_args(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in {"-c"}:
            continue
        if token == "-o":
            index += 1
            continue
        if token in {"-I", "-D", "-U", "-isystem"} and index < len(tokens):
            kept.extend([token, tokens[index]])
            index += 1
            continue
        if token.startswith(("-I", "-D", "-U", "-std=", "-isystem")):
            kept.append(token)
    return kept

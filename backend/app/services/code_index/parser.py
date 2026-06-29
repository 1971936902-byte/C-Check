from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache


PARSER_VERSION = "hybrid-c-parser-v3"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CONTROL_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "case",
    "do",
    "else",
}
_FUNCTION_HEADER_RE = re.compile(
    rf"^\s*(?P<prefix>[A-Za-z_][A-Za-z0-9_*\s]*?)"
    rf"[\s*]+(?P<name>{_IDENTIFIER})\s*\([^;]*\)\s*\{{"
)
_FUNCTION_DECL_RE = re.compile(
    rf"^\s*(?P<prefix>[A-Za-z_][A-Za-z0-9_*\s]*?)"
    rf"[\s*]+(?P<name>{_IDENTIFIER})\s*\([^;{{}}]*\)\s*;"
)
_GLOBAL_VAR_RE = re.compile(
    rf"^\s*[A-Za-z_][A-Za-z0-9_*\s]*\s+(?P<name>{_IDENTIFIER})\s*(?:=\s*[^;]+)?;"
)
_FUNCTION_POINTER_RE = re.compile(rf"^\s*(?:typedef\s+)?[\w\s*]+\(\s*\*\s*(?P<name>{_IDENTIFIER})\s*\)\s*\([^;]*\)\s*;")
_CALLBACK_BINDING_RE = re.compile(rf"\.\s*(?P<field>{_IDENTIFIER})\s*=\s*&?\s*(?P<target>{_IDENTIFIER})\b")
_CALL_RE = re.compile(rf"\b(?P<name>{_IDENTIFIER})\s*\(")
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"](?P<target>[^>"]+)[>"]')
_MACRO_RE = re.compile(rf"^\s*#\s*define\s+(?P<name>{_IDENTIFIER})\b")
_CONDITIONAL_RE = re.compile(rf"^\s*#\s*(?P<directive>if|ifdef|ifndef|elif)\b\s*(?P<expr>.*)")
_TYPE_RE = re.compile(rf"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(?P<name>{_IDENTIFIER})\b")
MAX_REGEX_LINE_LENGTH = 2000
MAX_FUNCTION_HEADER_LINES = 12
MAX_FUNCTION_HEADER_LENGTH = 4000


@dataclass(frozen=True)
class ParsedCall:
    caller_name: str
    callee_name: str
    line: int


@dataclass(frozen=True)
class ParsedInclude:
    target: str
    line: int


@dataclass(frozen=True)
class ParsedSymbol:
    kind: str
    name: str
    signature: str | None
    start_line: int
    end_line: int
    confidence: float
    source_tool: str = PARSER_VERSION


@dataclass(frozen=True)
class ParsedFile:
    relative_path: str
    line_count: int
    includes: list[ParsedInclude] = field(default_factory=list)
    symbols: list[ParsedSymbol] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)


def parse_c_source(relative_path: str, source_text: str) -> ParsedFile:
    return _parse_c_source_cached(relative_path, source_text)


@lru_cache(maxsize=1024)
def _parse_c_source_cached(relative_path: str, source_text: str) -> ParsedFile:
    tree_sitter_parsed = _tree_sitter_file(relative_path, source_text)
    libclang_parsed = _libclang_file(relative_path, source_text)
    fallback = _parse_c_source_builtin(relative_path, source_text)
    parsed_sources = [source for source in (tree_sitter_parsed, libclang_parsed, fallback) if source is not None]
    if not parsed_sources:
        return fallback
    primary = parsed_sources[0]
    for supplemental in parsed_sources[1:]:
        primary = ParsedFile(
            relative_path=relative_path,
            line_count=max(primary.line_count, supplemental.line_count),
            includes=_merge_includes(primary.includes, supplemental.includes),
            symbols=_merge_symbols(primary.symbols, supplemental.symbols),
            calls=_merge_calls(primary.calls, supplemental.calls),
        )
    return primary

def _parse_c_source_builtin(relative_path: str, source_text: str) -> ParsedFile:
    lines = source_text.splitlines()
    includes = _parse_includes(lines)
    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []

    symbols.extend(_parse_macro_symbols(lines))
    symbols.extend(_parse_conditional_symbols(lines))
    symbols.extend(_parse_type_symbols(lines))
    symbols.extend(_parse_function_pointer_symbols(lines))
    symbols.extend(_parse_callback_binding_symbols(lines))
    symbols.extend(_parse_declaration_symbols(lines))
    symbols.extend(_parse_global_variable_symbols(lines))
    function_symbols, function_calls = _parse_functions(lines)
    symbols.extend(function_symbols)
    calls.extend(function_calls)
    symbols = _merge_symbols(symbols, _ctags_symbols(relative_path, source_text))

    return ParsedFile(
        relative_path=relative_path,
        line_count=max(1, len(lines)),
        includes=includes,
        symbols=symbols,
        calls=calls,
    )


def _parse_includes(lines: list[str]) -> list[ParsedInclude]:
    includes: list[ParsedInclude] = []
    for line_number, line in enumerate(lines, start=1):
        match = _INCLUDE_RE.match(line)
        if match:
            includes.append(ParsedInclude(target=match.group("target"), line=line_number))
    return includes


def _parse_macro_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        match = _MACRO_RE.match(line)
        if match:
            symbols.append(
                ParsedSymbol(
                    kind="macro",
                    name=match.group("name"),
                    signature=line.strip(),
                    start_line=line_number,
                    end_line=line_number,
                    confidence=0.75,
                )
            )
    return symbols


def _parse_conditional_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        match = _CONDITIONAL_RE.match(line)
        if not match:
            continue
        expr = match.group("expr").strip()
        name = _conditional_name(match.group("directive"), expr, line_number)
        symbols.append(
            ParsedSymbol(
                kind="conditional",
                name=name,
                signature=line.strip(),
                start_line=line_number,
                end_line=line_number,
                confidence=0.55,
            )
        )
    return symbols


def _conditional_name(directive: str, expr: str, line_number: int) -> str:
    identifier_match = re.search(_IDENTIFIER, expr)
    if identifier_match:
        return f"{directive}_{identifier_match.group(0)}"
    return f"{directive}_line_{line_number}"


def _parse_type_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        match = _TYPE_RE.match(line)
        if match:
            symbols.append(
                ParsedSymbol(
                    kind="struct" if line.lstrip().startswith("struct") else "type",
                    name=match.group("name"),
                    signature=line.strip(),
                    start_line=line_number,
                    end_line=line_number,
                    confidence=0.65,
                )
            )
    return symbols


def _parse_function_pointer_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = _strip_line_comment(line).strip()
        if len(stripped) > MAX_REGEX_LINE_LENGTH:
            continue
        match = _FUNCTION_POINTER_RE.match(stripped)
        if not match:
            continue
        symbols.append(
            ParsedSymbol(
                kind="function_pointer",
                name=match.group("name"),
                signature=stripped,
                start_line=line_number,
                end_line=line_number,
                confidence=0.72,
            )
        )
    return symbols


def _parse_callback_binding_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = _strip_line_comment(line).strip()
        if len(stripped) > MAX_REGEX_LINE_LENGTH:
            continue
        for match in _CALLBACK_BINDING_RE.finditer(stripped):
            target = match.group("target")
            if target in _CONTROL_KEYWORDS:
                continue
            symbols.append(
                ParsedSymbol(
                    kind="callback_binding",
                    name=target,
                    signature=stripped,
                    start_line=line_number,
                    end_line=line_number,
                    confidence=0.68,
                )
            )
    return symbols


def _parse_declaration_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        line_without_comment = _strip_line_comment(line)
        if len(line_without_comment) > MAX_REGEX_LINE_LENGTH:
            continue
        match = _FUNCTION_DECL_RE.match(line_without_comment)
        if not match or match.group("name") in _CONTROL_KEYWORDS:
            continue
        symbols.append(
            ParsedSymbol(
                kind="declaration",
                name=match.group("name"),
                signature=line.strip(),
                start_line=line_number,
                end_line=line_number,
                confidence=0.70,
            )
        )
    return symbols


def _parse_global_variable_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    top_level_flags = _top_level_scope_flags(lines)
    for line_number, (line, is_top_level) in enumerate(zip(lines, top_level_flags, strict=True), start=1):
        if not is_top_level:
            continue
        stripped = _strip_line_comment(line).strip()
        if (
            not stripped
            or len(stripped) > MAX_REGEX_LINE_LENGTH
            or stripped.startswith("#")
            or "(" in stripped
            or stripped.startswith(("typedef", "return"))
        ):
            continue
        match = _GLOBAL_VAR_RE.match(stripped)
        if not match or match.group("name") in _CONTROL_KEYWORDS:
            continue
        symbols.append(
            ParsedSymbol(
                kind="global_variable",
                name=match.group("name"),
                signature=stripped,
                start_line=line_number,
                end_line=line_number,
                confidence=0.60,
            )
        )
    return symbols


def _top_level_scope_flags(lines: list[str]) -> list[bool]:
    flags: list[bool] = []
    depth = 0
    in_block_comment = False
    for line in lines:
        flags.append(depth == 0)
        if line.lstrip().startswith("#"):
            continue
        index = 0
        quote: str | None = None
        while index < len(line):
            char = line[index]
            following = line[index + 1] if index + 1 < len(line) else ""
            if in_block_comment:
                if char == "*" and following == "/":
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char == "/" and following == "*":
                in_block_comment = True
                index += 2
                continue
            if char == "/" and following == "/":
                break
            if char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth = max(0, depth - 1)
            index += 1
    return flags


def _parse_functions(lines: list[str]) -> tuple[list[ParsedSymbol], list[ParsedCall]]:
    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []
    line_count = len(lines)
    index = 0
    while index < line_count:
        header = _candidate_function_header(lines, index)
        if header is None:
            index += 1
            continue
        match = _FUNCTION_HEADER_RE.match(header)
        if not match or match.group("name") in _CONTROL_KEYWORDS:
            index += 1
            continue

        name = match.group("name")
        start_line = index + 1
        end_index = _find_balanced_block_end(lines, index)
        signature = header.strip().rstrip("{").strip()
        symbols.append(
            ParsedSymbol(
                kind="function",
                name=name,
                signature=signature,
                start_line=start_line,
                end_line=end_index + 1,
                confidence=0.85,
            )
        )
        for body_index in range(index, end_index + 1):
            for call_match in _CALL_RE.finditer(_strip_line_comment(lines[body_index])):
                callee = call_match.group("name")
                if callee in _CONTROL_KEYWORDS or callee == name:
                    continue
                calls.append(ParsedCall(caller_name=name, callee_name=callee, line=body_index + 1))
        index = end_index + 1
    return symbols, calls


def _candidate_function_header(lines: list[str], start_index: int) -> str | None:
    parts: list[str] = []
    total_length = 0
    for index in range(start_index, min(len(lines), start_index + MAX_FUNCTION_HEADER_LINES)):
        line = _strip_line_comment(lines[index]).strip()
        if not line:
            if parts:
                break
            continue
        if line.startswith("#"):
            return None
        total_length += len(line)
        if total_length > MAX_FUNCTION_HEADER_LENGTH or len(line) > MAX_REGEX_LINE_LENGTH:
            return None
        parts.append(line)
        joined = " ".join(parts)
        if ";" in line and "{" not in line:
            return None
        if "{" in line:
            before_brace = joined.split("{", 1)[0]
            if "=" in before_brace:
                return None
            return joined
    return None


def _find_balanced_block_end(lines: list[str], start_index: int) -> int:
    depth = 0
    started = False
    for index in range(start_index, len(lines)):
        line = _strip_line_comment(lines[index])
        for char in line:
            if char == "{":
                depth += 1
                started = True
            elif char == "}":
                depth -= 1
                if started and depth <= 0:
                    return index
    return start_index


def _strip_line_comment(line: str) -> str:
    return line.split("//", 1)[0]


def _ctags_symbols(relative_path: str, source_text: str) -> list[ParsedSymbol]:
    try:
        from app.services.code_index.ctags import extract_ctags_symbols

        return extract_ctags_symbols(relative_path, source_text)
    except Exception:
        return []


def _merge_symbols(primary: list[ParsedSymbol], supplemental: list[ParsedSymbol]) -> list[ParsedSymbol]:
    merged = list(primary)
    seen = {(symbol.kind, symbol.name, symbol.start_line) for symbol in merged}
    for symbol in supplemental:
        key = (symbol.kind, symbol.name, symbol.start_line)
        if key in seen:
            continue
        merged.append(symbol)
        seen.add(key)
    return merged


def _merge_includes(primary: list[ParsedInclude], supplemental: list[ParsedInclude]) -> list[ParsedInclude]:
    merged = list(primary)
    seen = {(include.target, include.line) for include in merged}
    for include in supplemental:
        key = (include.target, include.line)
        if key not in seen:
            merged.append(include)
            seen.add(key)
    return merged


def _merge_calls(primary: list[ParsedCall], supplemental: list[ParsedCall]) -> list[ParsedCall]:
    merged = list(primary)
    seen = {(call.caller_name, call.callee_name, call.line) for call in merged}
    for call in supplemental:
        key = (call.caller_name, call.callee_name, call.line)
        if key not in seen:
            merged.append(call)
            seen.add(key)
    return merged


def _tree_sitter_file(relative_path: str, source_text: str) -> ParsedFile | None:
    try:
        from app.services.code_index.tree_sitter_c import parse_with_tree_sitter

        return parse_with_tree_sitter(relative_path, source_text)
    except Exception:
        return None


def _libclang_file(relative_path: str, source_text: str) -> ParsedFile | None:
    try:
        from app.services.code_index.clangd import parse_with_libclang

        return parse_with_libclang(relative_path, source_text)
    except Exception:
        return None

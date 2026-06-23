from __future__ import annotations

import re
from dataclasses import dataclass, field


PARSER_VERSION = "builtin-c-parser-v1"
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
    rf"^\s*(?P<prefix>(?:static\s+|inline\s+|extern\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+|enum\s+|union\s+|[A-Za-z_][A-Za-z0-9_*\s]+)+?)"
    rf"\s+(?P<name>{_IDENTIFIER})\s*\([^;]*\)\s*\{{"
)
_FUNCTION_DECL_RE = re.compile(
    rf"^\s*(?P<prefix>(?:static\s+|inline\s+|extern\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+|enum\s+|union\s+|[A-Za-z_][A-Za-z0-9_*\s]+)+?)"
    rf"\s+(?P<name>{_IDENTIFIER})\s*\([^;{{}}]*\)\s*;"
)
_GLOBAL_VAR_RE = re.compile(
    rf"^\s*(?:extern\s+|static\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+|enum\s+|union\s+|[A-Za-z_][A-Za-z0-9_*\s]+)+\s+(?P<name>{_IDENTIFIER})\s*(?:=\s*[^;]+)?;"
)
_CALL_RE = re.compile(rf"\b(?P<name>{_IDENTIFIER})\s*\(")
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"](?P<target>[^>"]+)[>"]')
_MACRO_RE = re.compile(rf"^\s*#\s*define\s+(?P<name>{_IDENTIFIER})\b")
_TYPE_RE = re.compile(rf"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(?P<name>{_IDENTIFIER})\b")


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
    tree_sitter_parsed = _tree_sitter_file(relative_path, source_text)
    if tree_sitter_parsed is not None:
        fallback = _parse_c_source_builtin(relative_path, source_text)
        return ParsedFile(
            relative_path=relative_path,
            line_count=max(tree_sitter_parsed.line_count, fallback.line_count),
            includes=_merge_includes(tree_sitter_parsed.includes, fallback.includes),
            symbols=_merge_symbols(tree_sitter_parsed.symbols, fallback.symbols),
            calls=_merge_calls(tree_sitter_parsed.calls, fallback.calls),
        )
    return _parse_c_source_builtin(relative_path, source_text)


def _parse_c_source_builtin(relative_path: str, source_text: str) -> ParsedFile:
    lines = source_text.splitlines()
    includes = _parse_includes(lines)
    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []

    symbols.extend(_parse_macro_symbols(lines))
    symbols.extend(_parse_type_symbols(lines))
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


def _parse_declaration_symbols(lines: list[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for line_number, line in enumerate(lines, start=1):
        match = _FUNCTION_DECL_RE.match(_strip_line_comment(line))
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
    for line_number, line in enumerate(lines, start=1):
        stripped = _strip_line_comment(line).strip()
        if not stripped or stripped.startswith("#") or "(" in stripped or stripped.startswith(("typedef", "return")):
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


def _parse_functions(lines: list[str]) -> tuple[list[ParsedSymbol], list[ParsedCall]]:
    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []
    line_count = len(lines)
    index = 0
    while index < line_count:
        line = _strip_line_comment(lines[index])
        match = _FUNCTION_HEADER_RE.match(line)
        if not match or match.group("name") in _CONTROL_KEYWORDS:
            index += 1
            continue

        name = match.group("name")
        start_line = index + 1
        end_index = _find_balanced_block_end(lines, index)
        signature = line.strip().rstrip("{").strip()
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
        for body_index in range(index + 1, end_index + 1):
            for call_match in _CALL_RE.finditer(_strip_line_comment(lines[body_index])):
                callee = call_match.group("name")
                if callee in _CONTROL_KEYWORDS or callee == name:
                    continue
                calls.append(ParsedCall(caller_name=name, callee_name=callee, line=body_index + 1))
        index = end_index + 1
    return symbols, calls


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

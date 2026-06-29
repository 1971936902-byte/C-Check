from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TreeSitterStatus:
    available: bool
    reason: str | None = None


MAX_TREE_SITTER_SOURCE_BYTES = 256_000
MAX_TREE_SITTER_SOURCE_LINES = 180
MAX_TREE_SITTER_NODES = 20_000
MAX_DECLARATOR_SCAN_NODES = 512
MAX_DECLARATOR_SCAN_DEPTH = 64


class _TreeSitterBudgetExceeded(RuntimeError):
    pass


def probe_tree_sitter_c() -> TreeSitterStatus:
    try:
        import tree_sitter  # noqa: F401
    except Exception as exc:
        return TreeSitterStatus(available=False, reason=f"tree-sitter unavailable: {exc}")
    try:
        import tree_sitter_c  # noqa: F401
    except Exception as exc:
        return TreeSitterStatus(available=False, reason=f"tree-sitter-c unavailable: {exc}")
    return TreeSitterStatus(available=True)


def parse_with_tree_sitter(relative_path: str, source_text: str):
    status = probe_tree_sitter_c()
    if not status.available:
        return None
    source_bytes = source_text.encode("utf-8", errors="ignore")
    if len(source_bytes) > MAX_TREE_SITTER_SOURCE_BYTES or source_text.count("\n") + 1 > MAX_TREE_SITTER_SOURCE_LINES:
        return None
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_c

        from app.services.code_index.parser import ParsedCall, ParsedFile, ParsedInclude, ParsedSymbol
    except Exception:
        return None
    try:
        language = Language(tree_sitter_c.language())
        parser = Parser(language)
    except Exception:
        try:
            parser = Parser()
            parser.set_language(Language(tree_sitter_c.language()))
        except Exception:
            return None
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return None

    includes: list[ParsedInclude] = []
    symbols: list[ParsedSymbol] = []
    calls: list[ParsedCall] = []

    def text(node) -> str:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")

    def name_from_declarator(node, *, depth: int = 0, scanned: list[int] | None = None) -> str | None:
        scanned = scanned if scanned is not None else [0]
        scanned[0] += 1
        if depth > MAX_DECLARATOR_SCAN_DEPTH or scanned[0] > MAX_DECLARATOR_SCAN_NODES:
            return None
        if node.type == "identifier":
            return text(node)
        for child in getattr(node, "children", []):
            found = name_from_declarator(child, depth=depth + 1, scanned=scanned)
            if found:
                return found
        return None

    visited_nodes = 0

    def visit(node, current_function: str | None = None) -> None:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > MAX_TREE_SITTER_NODES:
            raise _TreeSitterBudgetExceeded
        node_type = node.type
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        if node_type == "preproc_include":
            raw = text(node)
            target = raw.split("include", 1)[-1].strip().strip("<>\"")
            if target:
                includes.append(ParsedInclude(target=target, line=start_line))
        elif node_type in {"preproc_def", "preproc_function_def"}:
            raw = text(node).strip()
            parts = raw.replace("(", " ").split()
            if len(parts) >= 2:
                symbols.append(
                    ParsedSymbol(
                        kind="macro",
                        name=parts[1],
                        signature=raw,
                        start_line=start_line,
                        end_line=end_line,
                        confidence=0.9,
                        source_tool="tree-sitter-c",
                    )
                )
        elif node_type == "function_definition":
            name = name_from_declarator(node.child_by_field_name("declarator") or node)
            if name:
                symbols.append(
                    ParsedSymbol(
                        kind="function",
                        name=name,
                        signature=text(node).split("{", 1)[0].strip(),
                        start_line=start_line,
                        end_line=end_line,
                        confidence=0.9,
                        source_tool="tree-sitter-c",
                    )
                )
                current_function = name
        elif node_type in {"struct_specifier", "union_specifier", "enum_specifier", "type_definition"}:
            if node_type != "type_definition" and node.child_by_field_name("body") is None:
                for child in getattr(node, "children", []):
                    visit(child, current_function)
                return
            name = name_from_declarator(node)
            if name:
                symbol_kind = {
                    "struct_specifier": "struct",
                    "union_specifier": "union",
                    "enum_specifier": "enum",
                    "type_definition": "typedef",
                }[node_type]
                symbols.append(
                    ParsedSymbol(
                        kind=symbol_kind,
                        name=name,
                        signature=text(node).splitlines()[0].strip(),
                        start_line=start_line,
                        end_line=end_line,
                        confidence=0.85,
                        source_tool="tree-sitter-c",
                    )
                )
        elif node_type == "call_expression" and current_function:
            name = name_from_declarator(node.child_by_field_name("function") or node)
            if name:
                calls.append(ParsedCall(caller_name=current_function, callee_name=name, line=start_line))
        for child in getattr(node, "children", []):
            visit(child, current_function)

    try:
        visit(tree.root_node)
    except _TreeSitterBudgetExceeded:
        return None
    if not symbols and not includes and not calls:
        return None
    return ParsedFile(
        relative_path=relative_path,
        line_count=max(1, len(source_text.splitlines())),
        includes=includes,
        symbols=symbols,
        calls=calls,
    )

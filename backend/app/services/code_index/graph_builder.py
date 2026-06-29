from __future__ import annotations

from app.db.models import CodeEdge, CodeFile, CodeProject, CodeSymbol
from app.services.code_index.parser import PARSER_VERSION, ParsedCall, ParsedFile


def build_symbol_edges(
    project: CodeProject,
    code_file: CodeFile,
    parsed: ParsedFile,
    file_symbols: list[CodeSymbol],
    symbols_by_name: dict[str, list[CodeSymbol]],
) -> list[CodeEdge]:
    symbol_by_name = {symbol.name: symbol for symbol in file_symbols}
    edges: list[CodeEdge] = []
    for parsed_symbol in parsed.symbols:
        symbol = symbol_by_name.get(parsed_symbol.name)
        if not symbol:
            continue
        edges.append(
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
        if parsed_symbol.kind == "function":
            edges.append(
                CodeEdge(
                    project=project,
                    source_id=symbol.id,
                    target_id=code_file.id,
                    edge_type="SYMBOL_DEFINED_IN",
                    line=parsed_symbol.start_line,
                    confidence=parsed_symbol.confidence,
                    source_tool=PARSER_VERSION,
                    metadata_json={"symbol_name": parsed_symbol.name, "file_path": code_file.relative_path},
                )
            )
        if parsed_symbol.kind == "declaration":
            target = _best_symbol_match(symbols_by_name.get(parsed_symbol.name, []), code_file.relative_path)
            edges.append(
                CodeEdge(
                    project=project,
                    source_id=symbol.id,
                    target_id=target.id if target else None,
                    edge_type="SYMBOL_DECLARED_IN",
                    line=parsed_symbol.start_line,
                    confidence=0.65 if target else 0.4,
                    source_tool=PARSER_VERSION,
                    metadata_json={"symbol_name": parsed_symbol.name},
                )
            )
        if parsed_symbol.kind == "callback_binding":
            target = _best_symbol_match(
                [candidate for candidate in symbols_by_name.get(parsed_symbol.name, []) if candidate.kind == "function"],
                code_file.relative_path,
            )
            edges.append(
                CodeEdge(
                    project=project,
                    source_id=symbol.id,
                    target_id=target.id if target else None,
                    edge_type="CALLBACK_BINDING_TARGETS_FUNCTION",
                    line=parsed_symbol.start_line,
                    confidence=0.70 if target else 0.45,
                    source_tool=PARSER_VERSION,
                    metadata_json={"callback_name": parsed_symbol.name},
                )
            )
    for call in parsed.calls:
        edges.extend(_call_edges(project, code_file, call, symbol_by_name, symbols_by_name))
    return edges


def build_include_edges(project: CodeProject, code_file: CodeFile, parsed: ParsedFile) -> list[CodeEdge]:
    return [
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
        for include in parsed.includes
    ]


def _call_edges(
    project: CodeProject,
    code_file: CodeFile,
    call: ParsedCall,
    symbol_by_name: dict[str, CodeSymbol],
    symbols_by_name: dict[str, list[CodeSymbol]],
) -> list[CodeEdge]:
    source_symbol = symbol_by_name.get(call.caller_name)
    if not source_symbol:
        return []
    target_symbol = _best_symbol_match(symbols_by_name.get(call.callee_name, []), code_file.relative_path)
    edges = [
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
    ]
    edges.append(
        CodeEdge(
            project=project,
            source_id=source_symbol.id,
            target_id=target_symbol.id if target_symbol else None,
            edge_type="CALLSITE_CALLS_SYMBOL",
            line=call.line,
            confidence=0.8 if target_symbol else 0.4,
            source_tool=PARSER_VERSION,
            metadata_json={"callee_name": call.callee_name},
        )
    )
    return edges


def build_usage_edges(project: CodeProject, chunk_symbols: list[CodeSymbol], symbols_by_name: dict[str, list[CodeSymbol]]) -> list[CodeEdge]:
    edges: list[CodeEdge] = []
    for symbol in chunk_symbols:
        if symbol.kind != "function":
            continue
        content = "\n".join(chunk.content for chunk in symbol.chunks if chunk.chunk_kind in {"function", "function_window"})
        for condition in (candidate for candidate in chunk_symbols if candidate.kind == "conditional"):
            edges.append(
                CodeEdge(
                    project=project,
                    source_id=symbol.id,
                    target_id=condition.id,
                    edge_type="FUNCTION_DEPENDS_ON_CONDITION",
                    line=symbol.start_line,
                    confidence=0.55,
                    source_tool=PARSER_VERSION,
                    metadata_json={"name": condition.name},
                )
            )
        for target_kinds, edge_type in (
            ({"macro"}, "FUNCTION_USES_MACRO"),
            ({"conditional"}, "FUNCTION_DEPENDS_ON_CONDITION"),
            ({"struct", "union", "typedef", "enum", "type"}, "FUNCTION_USES_TYPE"),
            ({"global_variable"}, "FUNCTION_USES_GLOBAL"),
            ({"function_pointer"}, "FUNCTION_USES_CALLBACK"),
        ):
            names = [
                name
                for name, candidates in symbols_by_name.items()
                if name != symbol.name
                and name in content
                and any(candidate.kind in target_kinds for candidate in candidates)
            ]
            for name in names:
                target = next((candidate for candidate in symbols_by_name.get(name, []) if candidate.kind in target_kinds), None)
                edges.append(
                    CodeEdge(
                        project=project,
                        source_id=symbol.id,
                        target_id=target.id if target else None,
                        edge_type=edge_type,
                        line=symbol.start_line,
                        confidence=0.65 if target else 0.4,
                        source_tool=PARSER_VERSION,
                        metadata_json={"name": name},
                    )
                )
        for name, candidates in symbols_by_name.items():
            if name == symbol.name or f"{name}(" not in content:
                continue
            target = next((candidate for candidate in candidates if candidate.kind in {"global_variable", "function_pointer"}), None)
            if target is None:
                continue
            edges.append(
                CodeEdge(
                    project=project,
                    source_id=symbol.id,
                    target_id=target.id,
                    edge_type="FUNCTION_USES_CALLBACK",
                    line=symbol.start_line,
                    confidence=0.70,
                    source_tool=PARSER_VERSION,
                    metadata_json={"name": name, "call_style": "indirect"},
                )
            )
    return edges


def _best_symbol_match(symbols: list[CodeSymbol], caller_path: str) -> CodeSymbol | None:
    if not symbols:
        return None
    return max(
        symbols,
        key=lambda symbol: (
            10 if symbol.kind == "function" else 0,
            5 if symbol.file and symbol.file.relative_path == caller_path else 0,
            2 if symbol.kind == "declaration" else 0,
            _path_relatedness(caller_path, symbol.file.relative_path if symbol.file else ""),
            symbol.confidence,
        ),
    )


def _path_relatedness(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_parts = [part for part in left.replace("\\", "/").split("/") if part]
    right_parts = [part for part in right.replace("\\", "/").split("/") if part]
    common_prefix = 0
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part != right_part:
            break
        common_prefix += 1
    left_stem = left_parts[-1].rsplit(".", 1)[0] if left_parts else ""
    right_stem = right_parts[-1].rsplit(".", 1)[0] if right_parts else ""
    stem_bonus = 1.0 if left_stem and left_stem == right_stem else 0.0
    return common_prefix + stem_bonus

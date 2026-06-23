from __future__ import annotations

from dataclasses import dataclass

from app.db.models import CodeChunk, CodeProject


@dataclass(frozen=True)
class ReviewUnit:
    unit_id: str
    unit_type: str
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int
    chunk_ids: list[str]


def plan_review_units(project: CodeProject) -> list[ReviewUnit]:
    units: list[ReviewUnit] = []
    chunks_by_symbol_name: dict[tuple[str, str], list[CodeChunk]] = {}
    for chunk in project.chunks:
        if chunk.symbol_name:
            chunks_by_symbol_name.setdefault((chunk.file.relative_path, chunk.symbol_name), []).append(chunk)
    for chunk in sorted(project.chunks, key=lambda item: (item.file.relative_path, item.start_line, item.end_line)):
        if chunk.chunk_kind == "function":
            units.append(_function_unit_from_chunk(chunk, chunks_by_symbol_name.get((chunk.file.relative_path, chunk.symbol_name or ""), [])))
        elif chunk.chunk_kind in {"file_summary", "callsite"}:
            units.append(_unit_from_chunk(chunk))
    return units


def _unit_from_chunk(chunk: CodeChunk) -> ReviewUnit:
    unit_type = "function" if chunk.chunk_kind == "function" else ("callsite" if chunk.chunk_kind == "callsite" else "file")
    return ReviewUnit(
        unit_id=f"{unit_type}:{chunk.file.relative_path}:{chunk.start_line}:{chunk.end_line}:{chunk.symbol_name or ''}",
        unit_type=unit_type,
        file_path=chunk.file.relative_path,
        symbol_name=chunk.symbol_name,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        chunk_ids=[chunk.id],
    )


def _function_unit_from_chunk(chunk: CodeChunk, related_chunks: list[CodeChunk]) -> ReviewUnit:
    related_ids = [
        related.id
        for related in sorted(related_chunks, key=lambda item: (item.start_line, item.end_line, item.chunk_kind))
        if related.chunk_kind in {"function", "function_window", "callsite"}
    ]
    if chunk.id not in related_ids:
        related_ids.insert(0, chunk.id)
    return ReviewUnit(
        unit_id=f"function:{chunk.file.relative_path}:{chunk.start_line}:{chunk.end_line}:{chunk.symbol_name or ''}",
        unit_type="function",
        file_path=chunk.file.relative_path,
        symbol_name=chunk.symbol_name,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        chunk_ids=related_ids,
    )

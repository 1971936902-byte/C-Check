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
    for chunk in sorted(project.chunks, key=lambda item: (item.file.relative_path, item.start_line, item.end_line)):
        if chunk.chunk_kind not in {"function", "file_summary", "callsite"}:
            continue
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

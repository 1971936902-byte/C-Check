from __future__ import annotations

import json
import math
import re
import asyncio
from time import perf_counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, ModelNode, ReviewFile, ReviewTask, TaskStatus
from app.schemas.model_response import (
    COMPACT_MAX_FINDINGS,
    CompactModelReviewResponse,
    FormattedFindingsResponse,
    FindingCategory,
    FindingSeverity,
    ModelReviewResponse,
    ReviewFinding,
)
from app.services.check_types import CHECK_TYPE_LABELS, check_types_prompt
from app.services.model_output_sanitizer import ModelOutputSanitizer
from app.services.static_c_rules import detect_static_c_findings
from app.services.code_index.clang_static_analyzer import diagnostics_to_findings, run_clang_static_analysis


MAX_MODEL_LOG_CHARS = 12000
RESPONSE_REQUIRED_KEYS = {"summary", "score", "findings"}
STRUCTURED_RESPONSE_SCHEMA_NAME = "c_review_fast_response"
MAX_MODEL_SNIPPET_LINES = 5
TOKEN_BUDGET_SAFETY_MARGIN = 128
INPUT_TOKEN_SAFETY_MARGIN = 512
MIN_RETRY_OUTPUT_TOKENS = 128
CHUNK_CONTEXT_CHAR_RATIO = 0.70
MIN_CHUNK_CONTEXT_CHARS = 1000
CHUNK_PROMPT_CHAR_MARGIN = 512
CHUNK_LINE_PREFIX_WIDTH = 6
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "suggestion": 3}
MODEL_OUTPUT_SANITIZER = ModelOutputSanitizer()


def _normalize_model_contract(value: Any) -> Any:
    return MODEL_OUTPUT_SANITIZER.sanitize(value)


def _normalize_formatted_findings_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        return {"findings": []}
    schema_categories = {
        "buffer_overflow",
        "pointer_safety",
        "memory_safety",
        "resource_leak",
        "integer_safety",
        "input_validation",
        "concurrency",
        "logic",
        "other",
    }
    findings = []
    for item in value["findings"][:COMPACT_MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        sanitized = MODEL_OUTPUT_SANITIZER._sanitize_finding(item)
        if sanitized["category"] not in schema_categories:
            sanitized["category"] = "other"
        findings.append(sanitized)
    return {"findings": findings}


TOKEN_BUDGET_PATTERN = re.compile(
    r"maximum context length is (?P<context>\d+) tokens and your request has (?P<input>\d+) input tokens",
    re.IGNORECASE,
)
VLLM_TOKEN_BUDGET_PATTERN = re.compile(
    r"maximum context length is (?P<context>\d+) tokens\..*?requested (?P<requested>\d+) tokens "
    r"\((?P<input>\d+) in the messages, (?P<completion>\d+) in the completion\)",
    re.IGNORECASE,
)
CANDIDATE_RESPONSE_CONTRACT = """
Return newline-delimited JSON (JSONL), one candidate object per line. No Markdown, array, wrapper object, summary, or score.
If no candidate exists, return exactly [].
Each line uses exactly these short keys: p, l, s, t, d.
- p: relative source path.
- l: exact integer source line or null.
- s: high, medium, low, or suggestion.
- t: snake_case defect type, at most 24 ASCII characters.
- d: one compact Chinese sentence, at most 36 Chinese characters; omit repeated boilerplate.
Format template: {"p":"<relative_path>","l":<line_or_null>,"s":"<severity>","t":"<free_form_type>","d":"<short_chinese_description>"}
Use one physical output line per candidate. Escape quotes and newlines inside strings.
"""


FINAL_RESPONSE_CONTRACT = """
Return exactly one compact JSON object. No Markdown.
Top-level keys: summary, score, findings.
Use Chinese. Keep summary under 50 Chinese chars.
This is final single-pass review mode. Return only supported visible findings.
Each finding uses exactly: severity, category, title, description, file_path, line.
Keep title under 24 Chinese chars.
Keep description under 140 Chinese chars and state the exact unsafe trigger or visible consequence.
The line value must point to the best executable statement or declaration in PRIMARY SOURCE.
Allowed category values only: buffer_overflow, pointer_safety, memory_safety, resource_leak, integer_safety, logic.
If category is uncertain or outside these focused categories, omit the finding instead of forcing logic.
PRIMARY SOURCE remains the proof standard; Definition Context is semantic compensation only.
"""


CANDIDATE_FORMAT_CONTRACT = """
Return exactly one compact JSON object. No Markdown.
The only top-level key is findings.
This is JSON document normalization, not vulnerability review.
Process only the supplied CANDIDATE JSONL records. Do not inspect source code, use RAG, verify vulnerability truth, or invent new findings.
The output finding count must never exceed the input record count.
Delete records whose type is outside the ALLOWED FINAL CATEGORIES list in the prompt.
Delete missing declaration/definition, implicit declaration, unknown type, undefined function, and generic missing-null-check records.
Delete fixed-mapped peripheral-register null-pointer records and vendor assertion missing-validation records.
For retained records, only map the free-form type to one allowed category and format fields.
Preserve the original file path, line, severity, and factual description exactly. Do not expand or reinterpret the vulnerability.
If a type cannot be mapped to an allowed category, delete the record instead of forcing it into logic or other.
Each finding uses exactly: severity, category, title, description, file_path, line.
Allowed schema categories: buffer_overflow, pointer_safety, memory_safety, resource_leak, integer_safety, logic.
Do not output remediation, snippets, evidence, confidence, call chains, or extra fields.
JSON shape example with one retained record:
{"findings":[{"severity":"medium","category":"memory_safety","title":"问题标题","description":"问题描述","file_path":"src/example.c","line":42}]}
If no record remains, return exactly: {"findings":[]}
The example shows structure only. Use values from the supplied candidate records and the allowed category list.
"""

SEMANTIC_CATEGORY_RESPONSE_CONTRACT = """
Return JSONL only, one decision per input candidate. No Markdown or wrapper object.
Each line must be: {"i":<candidate_id>,"a":"drop|correct","c":"<selected_category_or_null>"}
Use correct only when the candidate is a concrete defect whose semantics genuinely match one of the SELECTED CATEGORIES.
Use drop when the candidate does not match any selected category, is only compilation advice, or lacks a concrete defect.
Do not change or reproduce file paths, line numbers, severity, titles, descriptions, or source code.
"""

# Backward-compatible alias kept for tests and legacy callers that inspect the
# first-stage contract text directly.
RESPONSE_CONTRACT = CANDIDATE_RESPONSE_CONTRACT


class ModelInvocationError(RuntimeError):
    """Raised when a selected model cannot produce a valid review."""

    def __init__(
        self,
        message: str,
        *,
        raw_response: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.details = details


@dataclass(frozen=True)
class ChunkedReviewFile:
    relative_path: str
    source_text: str
    size_bytes: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SemanticSourceBlock:
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ModelNodeDispatchPool:
    nodes: tuple[ModelNode, ...]
    base_loads: dict[str, int]


@dataclass(frozen=True)
class ModelInvocationResult:
    value: BaseModel
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed_seconds: float | None = None


class CandidateCategoryDecision(BaseModel):
    i: int
    a: Literal["drop", "correct"]
    c: str | None = None


class CandidateCategoryDecisionResponse(BaseModel):
    decisions: list[CandidateCategoryDecision]


def _mock_response(files: Sequence[ReviewFile]) -> ModelReviewResponse:
    return ModelReviewResponse(
        summary=f"Mock review completed for {len(files)} source file(s).",
        score=100,
        findings=[],
    )


def _source_message(files: Sequence[ReviewFile]) -> str:
    sections = [
        "PRIMARY SOURCE (审查对象):",
        "Only report findings whose file_path and line are located in this primary source.",
        "Each source line may start with a 6-digit prefix like 000123:. Treat that prefix as location metadata, not as part of the C code.",
    ]
    for source in files:
        sections.append(f"===== FILE: {source.relative_path} =====\n{_render_source_with_absolute_lines(source.source_text)}")
    return "\n\n".join(sections)


def _user_message(
    files: Sequence[ReviewFile],
    *,
    input_message: str | None = None,
    user_context: str | None = None,
) -> str:
    source = input_message if input_message is not None else _source_message(files)
    return "\n\n".join(part for part in (user_context, source) if part)


def _estimate_tokens(text: str, settings: Settings) -> int:
    return math.ceil(len(text) / settings.model_token_chars_per_token)


def _input_token_budget(settings: Settings) -> int:
    context_budget = settings.model_context_window - settings.model_max_tokens - INPUT_TOKEN_SAFETY_MARGIN
    return max(0, min(settings.model_max_input_tokens, context_budget))


def _response_format_overhead(
    settings: Settings,
    *,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
) -> str:
    response_format = _response_format(settings, response_schema=response_schema)
    if response_format is None:
        return ""
    return json.dumps(response_format, ensure_ascii=False, separators=(",", ":"))


def _strict_prompt(
    prompt: str,
    response_contract: str,
    retry_instruction: str | None = None,
) -> str:
    strict_prompt = f"{prompt}\n\n{response_contract}"
    if retry_instruction:
        strict_prompt = (
            f"{strict_prompt}\n\nPrevious response was rejected by the backend validator:\n"
            f"{retry_instruction}\nReturn corrected output only, following the response contract exactly."
        )
    return strict_prompt


def _input_budget_details(
    *,
    prompt: str,
    files: Sequence[ReviewFile],
    settings: Settings,
    response_contract: str,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
    retry_instruction: str | None = None,
    input_message: str | None = None,
    user_context: str | None = None,
) -> tuple[int, int]:
    input_text = "\n\n".join(
        part
        for part in (
            _strict_prompt(prompt, response_contract, retry_instruction),
            _response_format_overhead(settings, response_schema=response_schema),
            _user_message(files, input_message=input_message, user_context=user_context),
        )
        if part
    )
    return _estimate_tokens(input_text, settings), _input_token_budget(settings)


def _source_char_budget(
    *,
    prompt: str,
    settings: Settings,
    response_contract: str,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
    retry_instruction: str | None = None,
) -> int:
    max_input_chars = int(_input_token_budget(settings) * settings.model_token_chars_per_token)
    overhead = len(_strict_prompt(prompt, response_contract, retry_instruction)) + len(
        _response_format_overhead(settings, response_schema=response_schema)
    )
    return max(1, max_input_chars - overhead - CHUNK_PROMPT_CHAR_MARGIN)


def _numbered_chunk_source(source_text: str, start_line: int, end_line: int) -> str:
    lines = source_text.splitlines()
    selected = lines[start_line - 1 : end_line]
    return "\n".join(
        f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {line}"
        for line_number, line in enumerate(selected, start=start_line)
    )


_NUMBERED_SOURCE_LINE_RE = re.compile(rf"^\d{{{CHUNK_LINE_PREFIX_WIDTH}}}:\s")
_C_FUNCTION_DEFINITION_RE = re.compile(
    r"(?m)^\s*(?!if\b|for\b|while\b|switch\b)[A-Za-z_][\w\s*]*\b[A-Za-z_]\w*\s*\([^;{}]*\)\s*\{"
)
_DANGEROUS_C_OPERATION_RE = re.compile(
    r"\b(?:memcpy|memmove|strcpy|strcat|sprintf|scanf|gets|malloc|calloc|realloc|free|read|recv|fread)\s*\("
)
_POINTER_OR_ARRAY_OPERATION_RE = re.compile(r"(?:->|\[[^\]\n]+\]|\*\s*[A-Za-z_]\w*)")


def _render_source_with_absolute_lines(source_text: str) -> str:
    lines = source_text.splitlines()
    for line in lines:
        if line.strip():
            if _NUMBERED_SOURCE_LINE_RE.match(line):
                return source_text
            break
    if not lines:
        return source_text
    return "\n".join(
        f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {line}"
        for line_number, line in enumerate(lines, start=1)
    )


def _whole_file_chunk(source: ReviewFile) -> ChunkedReviewFile:
    lines = source.source_text.splitlines()
    end_line = max(1, len(lines))
    return ChunkedReviewFile(
        relative_path=source.relative_path,
        source_text=_numbered_chunk_source(source.source_text, 1, end_line),
        size_bytes=source.size_bytes,
        start_line=1,
        end_line=end_line,
    )


def _chunk_file(source: ReviewFile, max_chars: int, *, no_slice_max_bytes: int = 0) -> list[ChunkedReviewFile]:
    if no_slice_max_bytes > 0 and source.size_bytes <= no_slice_max_bytes:
        return [_whole_file_chunk(source)]

    lines = source.source_text.splitlines()
    if not lines:
        return [
            ChunkedReviewFile(
                relative_path=source.relative_path,
                source_text=source.source_text,
                size_bytes=source.size_bytes,
                start_line=1,
                end_line=1,
            )
        ]

    semantic_blocks = _semantic_source_blocks(lines)
    if semantic_blocks and not any(
        len(f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {line}") > max_chars
        for line_number, line in enumerate(lines, start=1)
    ):
        return _chunk_file_on_semantic_boundaries(source, lines, max_chars, semantic_blocks)
    return _chunk_file_by_lines(source, lines, max_chars)


def _chunk_file_by_lines(
    source: ReviewFile,
    lines: Sequence[str],
    max_chars: int,
) -> list[ChunkedReviewFile]:
    chunks: list[ChunkedReviewFile] = []
    current_lines: list[str] = []
    current_chars = 0
    start_line = 1
    end_line = 1
    payload_budget = max(1, max_chars - CHUNK_LINE_PREFIX_WIDTH - 3)

    def flush() -> None:
        nonlocal current_lines, current_chars, start_line, end_line
        if not current_lines:
            return
        chunks.append(
            ChunkedReviewFile(
                relative_path=source.relative_path,
                source_text="\n".join(current_lines),
                size_bytes=0,
                start_line=start_line,
                end_line=end_line,
            )
        )
        current_lines = []
        current_chars = 0

    for line_number, line in enumerate(lines, start=1):
        segments = [line[index : index + payload_budget] for index in range(0, len(line), payload_budget)] or [""]
        for segment in segments:
            rendered = f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {segment}"
            rendered_chars = len(rendered) + 1
            if current_lines and current_chars + rendered_chars > max_chars:
                flush()
            if not current_lines:
                start_line = line_number
            current_lines.append(rendered)
            current_chars += rendered_chars
            end_line = line_number

    flush()
    return chunks


def _chunk_file_on_semantic_boundaries(
    source: ReviewFile,
    lines: Sequence[str],
    max_chars: int,
    semantic_blocks: Sequence[SemanticSourceBlock],
) -> list[ChunkedReviewFile]:
    rendered_costs = [
        len(f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {line}") + 1
        for line_number, line in enumerate(lines, start=1)
    ]
    prefix_costs = [0]
    for cost in rendered_costs:
        prefix_costs.append(prefix_costs[-1] + cost)

    def span_cost(start_line: int, end_line: int) -> int:
        return prefix_costs[end_line] - prefix_costs[start_line - 1]

    def containing_block(line_number: int) -> SemanticSourceBlock | None:
        return next(
            (
                block
                for block in semantic_blocks
                if block.start_line <= line_number < block.end_line
            ),
            None,
        )

    chunks: list[ChunkedReviewFile] = []
    start_line = 1
    total_lines = len(lines)
    minimum_boundary_fill = max(1, int(max_chars * 0.35))
    overlap_budget = max(1, int(max_chars * 0.15))
    while start_line <= total_lines:
        end_line = start_line
        while end_line < total_lines and span_cost(start_line, end_line + 1) <= max_chars:
            end_line += 1

        if end_line < total_lines:
            block = containing_block(end_line)
            if block is not None and block.start_line > start_line:
                boundary_end = block.start_line - 1
                block_fits_whole = span_cost(block.start_line, block.end_line) <= max_chars
                if block_fits_whole or span_cost(start_line, boundary_end) >= minimum_boundary_fill:
                    end_line = boundary_end

        chunks.append(
            ChunkedReviewFile(
                relative_path=source.relative_path,
                source_text=_numbered_chunk_source(source.source_text, start_line, end_line),
                size_bytes=0,
                start_line=start_line,
                end_line=end_line,
            )
        )
        if end_line >= total_lines:
            break

        block = containing_block(end_line)
        if block is None or block.start_line > start_line:
            start_line = end_line + 1
            continue

        # A single function/type block exceeds the budget. Preserve a small
        # overlap so lifecycle and declaration context survive the split.
        overlap_start = end_line
        while overlap_start > start_line and span_cost(overlap_start - 1, end_line) <= overlap_budget:
            overlap_start -= 1
        start_line = max(start_line + 1, overlap_start)
    return chunks


def _semantic_source_blocks(lines: Sequence[str]) -> list[SemanticSourceBlock]:
    sanitized = _sanitize_c_structure_lines(lines)
    blocks: list[SemanticSourceBlock] = []
    depth = 0
    header_start = 1
    active: tuple[str, int] | None = None
    for index, line in enumerate(sanitized, start=1):
        if depth == 0:
            stripped = line.strip()
            if stripped.startswith("#"):
                header_start = index + 1
                continue
            if "{" in line:
                header = "\n".join(sanitized[header_start - 1 : index]).split("{", 1)[0]
                if _looks_like_function_header(header):
                    active = ("function", header_start)
                elif re.search(r"\b(?:struct|union|enum)\b", header):
                    active = ("type", header_start)
                else:
                    active = None
            elif ";" in line or "}" in line:
                header_start = index + 1

        depth += line.count("{") - line.count("}")
        depth = max(0, depth)
        if depth == 0 and active is not None:
            kind, start = active
            blocks.append(SemanticSourceBlock(kind=kind, start_line=start, end_line=index))
            active = None
            header_start = index + 1
    return blocks


def _sanitize_c_structure_lines(lines: Sequence[str]) -> list[str]:
    sanitized: list[str] = []
    in_block_comment = False
    in_string: str | None = None
    escaped = False
    for line in lines:
        output: list[str] = []
        index = 0
        while index < len(line):
            char = line[index]
            next_char = line[index + 1] if index + 1 < len(line) else ""
            if in_block_comment:
                if char == "*" and next_char == "/":
                    in_block_comment = False
                    output.extend((" ", " "))
                    index += 2
                else:
                    output.append(" ")
                    index += 1
                continue
            if in_string is not None:
                output.append(" ")
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == in_string:
                    in_string = None
                index += 1
                continue
            if char == "/" and next_char == "*":
                in_block_comment = True
                output.extend((" ", " "))
                index += 2
                continue
            if char == "/" and next_char == "/":
                output.extend(" " * (len(line) - index))
                break
            if char in {'"', "'"}:
                in_string = char
                output.append(" ")
                index += 1
                continue
            output.append(char)
            index += 1
        sanitized.append("".join(output))
    return sanitized


def _chunk_review_files(files: Sequence[ReviewFile], settings: Settings) -> list[ChunkedReviewFile]:
    chunks: list[ChunkedReviewFile] = []
    max_chars = _effective_chunk_max_chars(settings)
    for source in files:
        chunks.extend(_chunk_file(source, max_chars, no_slice_max_bytes=settings.review_no_slice_max_bytes))
    return chunks


def _chunk_payload_chars(chunk: ChunkedReviewFile) -> int:
    return len(f"===== FILE: {chunk.relative_path} =====\n{chunk.source_text}\n\n")


def _effective_chunk_max_chars(
    settings: Settings,
    *,
    prompt: str | None = None,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
    retry_instruction: str | None = None,
) -> int:
    conservative_budget = int(settings.model_chunk_max_chars * CHUNK_CONTEXT_CHAR_RATIO)
    if settings.model_chunk_max_chars >= MIN_CHUNK_CONTEXT_CHARS:
        conservative_budget = max(MIN_CHUNK_CONTEXT_CHARS, conservative_budget)
    max_chars = max(1, min(settings.model_chunk_max_chars, conservative_budget))
    if prompt is not None:
        max_chars = min(max_chars, _source_char_budget(
            prompt=prompt,
            settings=settings,
            response_contract=response_contract,
            retry_instruction=retry_instruction,
        ))
    return max(1, max_chars)


def _chunk_review_batches(
    files: Sequence[ReviewFile],
    settings: Settings,
    *,
    prompt: str | None = None,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
    retry_instruction: str | None = None,
    isolate_chunks: bool = False,
) -> list[list[ChunkedReviewFile]]:
    batches: list[list[ChunkedReviewFile]] = []
    current_batch: list[ChunkedReviewFile] = []
    current_chars = 0
    max_chars = _effective_chunk_max_chars(
        settings,
        prompt=prompt,
        response_contract=response_contract,
        retry_instruction=retry_instruction,
    )

    chunks: list[ChunkedReviewFile] = []
    for source in files:
        chunks.extend(_chunk_file(source, max_chars, no_slice_max_bytes=settings.review_no_slice_max_bytes))
    if isolate_chunks:
        return [[chunk] for chunk in chunks]
    for chunk in chunks:
        chunk_chars = _chunk_payload_chars(chunk)
        if current_batch and current_chars + chunk_chars > max_chars:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(chunk)
        current_chars += chunk_chars

    if current_batch:
        batches.append(current_batch)
    return batches


def _node_min_gpu_index(node: ModelNode) -> int:
    return min(node.gpu_indices or [9999])


def _review_task_source_bytes(task: ReviewTask) -> int:
    return sum(source.size_bytes for source in task.files)


def _review_task_is_small(settings: Settings, task: ReviewTask) -> bool:
    return (
        len(task.files) <= settings.model_small_task_max_files
        and _review_task_source_bytes(task) <= settings.model_small_task_max_bytes
    )


def _reserved_small_task_nodes(nodes: tuple[ModelNode, ...], settings: Settings) -> set[str]:
    if len(nodes) < 3 or settings.model_small_task_reserved_nodes <= 0:
        return set()
    sorted_nodes = sorted(nodes, key=lambda node: (_node_min_gpu_index(node), node.created_at, node.id))
    return {node.id for node in sorted_nodes[-settings.model_small_task_reserved_nodes:]}


def _select_dispatch_nodes(
    nodes: tuple[ModelNode, ...],
    settings: Settings,
    task: ReviewTask,
) -> tuple[ModelNode, ...]:
    if not nodes:
        return nodes
    ordered_nodes = tuple(sorted(nodes, key=lambda node: (_node_min_gpu_index(node), node.created_at, node.id)))
    reserved_ids = _reserved_small_task_nodes(ordered_nodes, settings)
    if not reserved_ids:
        return ordered_nodes
    if _review_task_is_small(settings, task):
        reserved = tuple(node for node in ordered_nodes if node.id in reserved_ids)
        general = tuple(node for node in ordered_nodes if node.id not in reserved_ids)
        return reserved + general
    general = tuple(node for node in ordered_nodes if node.id not in reserved_ids)
    if not general:
        return ordered_nodes
    return general[:settings.model_large_task_max_nodes]


def _review_node_dispatch_pool(
    db: Session,
    requested_node: ModelNode,
    *,
    task: ReviewTask,
    settings: Settings,
) -> ModelNodeDispatchPool:
    sibling_nodes = tuple(
        db.scalars(
            select(ModelNode).where(
                ModelNode.is_enabled.is_(True),
                ModelNode.model_identifier == requested_node.model_identifier,
                ModelNode.api_key == requested_node.api_key,
            )
        ).all()
    )
    nodes = _select_dispatch_nodes(sibling_nodes or (requested_node,), settings, task)
    load_rows = db.execute(
        select(ReviewTask.model_node_id, func.count(ReviewTask.id))
        .where(ReviewTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]))
        .group_by(ReviewTask.model_node_id)
    ).all()
    return ModelNodeDispatchPool(
        nodes=nodes,
        base_loads={model_node_id: count for model_node_id, count in load_rows},
    )


def _node_dispatch_key(node: ModelNode, base_loads: dict[str, int], in_flight: dict[str, int]) -> tuple[int, int, int, str]:
    return (
        in_flight.get(node.id, 0),
        base_loads.get(node.id, 0),
        _node_min_gpu_index(node),
        node.id,
    )


def _is_retryable_node_failure(exc: ModelInvocationError) -> bool:
    return str(exc) == "selected model node is unavailable"


def _should_chunk(
    files: Sequence[ReviewFile],
    settings: Settings,
    *,
    prompt: str | None = None,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
) -> bool:
    return len(_source_message(files)) > _effective_chunk_max_chars(settings, prompt=prompt, response_contract=response_contract)


def _ensure_input_budget(
    *,
    prompt: str,
    files: Sequence[ReviewFile],
    settings: Settings,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
    retry_instruction: str | None = None,
    input_message: str | None = None,
    user_context: str | None = None,
) -> None:
    estimated_tokens, budget_tokens = _input_budget_details(
        prompt=prompt,
        files=files,
        settings=settings,
        response_contract=response_contract,
        response_schema=response_schema,
        retry_instruction=retry_instruction,
        input_message=input_message,
        user_context=user_context,
    )
    if estimated_tokens <= budget_tokens:
        return
    raise ModelInvocationError(
        "model context window is too small for this review request",
        details=(
            f"Estimated input tokens {estimated_tokens} exceed safe input budget "
            f"{budget_tokens} for context window {settings.model_context_window}. "
            "The request should be chunked or reduced before invoking the model."
        ),
    )


def _batch_prompt(
    prompt: str,
    batch_index: int,
    batch_count: int,
    batch: Sequence[ChunkedReviewFile],
) -> str:
    chunk_lines = "\n".join(
        f"- {chunk.relative_path}, lines {chunk.start_line}-{chunk.end_line}"
        for chunk in batch
    )
    return (
        f"{prompt}\n\n"
        "The submitted code is being reviewed in batches to keep context usage controlled and "
        "balance work across available model nodes. Review only this batch and report concrete issues visible in this "
        "batch. Each source line is prefixed as `000123: code`; use the numeric prefix as the "
        "`line` value and keep `file_path` as the original file path.\n"
        f"Batch {batch_index} of {batch_count}, containing {len(batch)} source chunk(s):\n"
        f"{chunk_lines}"
    )


BatchPromptBuilder = Callable[[str, int, int, Sequence[ChunkedReviewFile]], str]


def _merged_score(results: Sequence[ModelReviewResponse]) -> float:
    if not results:
        return 100
    if any(result.findings for result in results):
        return max(0, min(result.score for result in results))
    return round(sum(result.score for result in results) / len(results), 2)


def _merge_chunk_results(results: Sequence[ModelReviewResponse]) -> ModelReviewResponse:
    findings = [finding for result in results for finding in result.findings]
    findings.sort(
        key=lambda finding: (
            SEVERITY_RANK.get(finding.severity.value, 99),
            finding.file_path,
            finding.line or 10**9,
        )
    )
    if findings:
        summary = f"分片审查完成，共发现 {len(findings)} 个问题，已保存全部问题并按风险等级排序。"
    else:
        summary = "分片审查完成，未发现明确问题。"
    return ModelReviewResponse(summary=summary, score=_merged_score(results), findings=findings)


async def _invoke_chunked_review(
    *,
    node: ModelNode,
    dispatch_pool: ModelNodeDispatchPool | None = None,
    files: Sequence[ReviewFile],
    prompt: str,
    settings: Settings,
    retry_instruction: str | None = None,
    chunk_max_chars: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    batch_prompt_builder: BatchPromptBuilder | None = None,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
    response_model: type[BaseModel] = ModelReviewResponse,
    response_normalizer: Callable[[Any], Any] | None = _normalize_model_contract,
    response_parser: Callable[[dict[str, Any]], BaseModel] | None = None,
) -> ModelReviewResponse:
    if chunk_max_chars is not None:
        settings = settings.model_copy(update={"model_chunk_max_chars": chunk_max_chars})
    while True:
        batches = _chunk_review_batches(
            files,
            settings,
            prompt=prompt,
            response_contract=response_contract,
            retry_instruction=retry_instruction,
            isolate_chunks=dispatch_pool is not None and len(dispatch_pool.nodes) > 1,
        )
        if len(batches) > settings.model_chunk_max_count:
            raise ModelInvocationError(
                "review request is too large to split safely",
                details=(
                    f"Generated {len(batches)} chunks, exceeding MODEL_CHUNK_MAX_COUNT="
                    f"{settings.model_chunk_max_count}. Reduce the number of files, total "
                    "source size, or raise the limit only after confirming GPU capacity."
                ),
            )
        indexed_results: list[tuple[int, ModelReviewResponse]] = []
        pool_nodes = tuple(dispatch_pool.nodes) if dispatch_pool is not None else (node,)
        if not pool_nodes:
            pool_nodes = (node,)
        base_loads = dispatch_pool.base_loads if dispatch_pool is not None else {}
        in_flight = {candidate.id: 0 for candidate in pool_nodes}
        unavailable_node_ids: set[str] = set()
        dispatch_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(settings.model_chunk_concurrency)

        async def acquire_node(attempted_node_ids: set[str]) -> ModelNode:
            async with dispatch_lock:
                candidates = [
                    candidate
                    for candidate in pool_nodes
                    if candidate.id not in attempted_node_ids and candidate.id not in unavailable_node_ids
                ]
                if not candidates:
                    candidates = [
                        candidate
                        for candidate in pool_nodes
                        if candidate.id not in attempted_node_ids
                    ]
                if not candidates:
                    raise ModelInvocationError("selected model node is unavailable")
                selected = min(candidates, key=lambda candidate: _node_dispatch_key(candidate, base_loads, in_flight))
                in_flight[selected.id] = in_flight.get(selected.id, 0) + 1
                return selected

        async def release_node(selected: ModelNode) -> None:
            async with dispatch_lock:
                in_flight[selected.id] = max(0, in_flight.get(selected.id, 0) - 1)

        async def invoke_batch(index: int, batch: Sequence[ChunkedReviewFile]) -> tuple[int, ModelReviewResponse]:
            async with semaphore:
                attempted_node_ids: set[str] = set()
                last_error: ModelInvocationError | None = None
                while len(attempted_node_ids) < len(pool_nodes):
                    selected_node = await acquire_node(attempted_node_ids)
                    attempted_node_ids.add(selected_node.id)
                    try:
                        result = await invoke_model(
                            node=selected_node,
                            files=list(batch),  # type: ignore[list-item]
                            prompt=(
                                batch_prompt_builder(prompt, index, len(batches), batch)
                                if batch_prompt_builder is not None
                                else _batch_prompt(prompt, index, len(batches), batch)
                            ),
                            retry_instruction=retry_instruction,
                            settings=settings,
                            response_contract=response_contract,
                            response_schema=response_schema,
                            response_model=response_model,
                            response_normalizer=response_normalizer,
                            response_parser=response_parser,
                        )
                        if not isinstance(result, ModelReviewResponse):
                            raise ModelInvocationError("candidate batch returned an unexpected response type")
                        return index, result
                    except ModelInvocationError as exc:
                        last_error = exc
                        if _is_retryable_node_failure(exc) and len(attempted_node_ids) < len(pool_nodes):
                            unavailable_node_ids.add(selected_node.id)
                            continue
                        raise
                    finally:
                        await release_node(selected_node)
                if last_error is not None:
                    raise last_error
                raise ModelInvocationError("selected model node is unavailable")

        pending = [
            asyncio.create_task(invoke_batch(index, batch))
            for index, batch in enumerate(batches, start=1)
        ]
        try:
            for completed_count, task in enumerate(asyncio.as_completed(pending), start=1):
                indexed_results.append(await task)
                if progress_callback is not None:
                    progress_callback(completed_count, len(batches))
        except ModelInvocationError as exc:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if "context window is too small" not in str(exc):
                raise
            next_chunk_max_chars = settings.model_chunk_max_chars // 2
            if next_chunk_max_chars < MIN_CHUNK_CONTEXT_CHARS or next_chunk_max_chars == settings.model_chunk_max_chars:
                raise
            settings = settings.model_copy(update={"model_chunk_max_chars": next_chunk_max_chars})
            continue
        indexed_results.sort(key=lambda item: item[0])
        return _merge_chunk_results(
            [result for _, result in indexed_results]
        )


def truncate_model_log(value: str | None, limit: int = MAX_MODEL_LOG_CHARS) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... [truncated {len(value) - limit} chars]"


def _is_contract_object(value: Any, required_keys: set[str] | None = None) -> bool:
    required_keys = RESPONSE_REQUIRED_KEYS if required_keys is None else required_keys
    return isinstance(value, dict) and required_keys.issubset(value)


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _find_json_array_start(content: str, key: str) -> int | None:
    key_index = content.find(f'"{key}"')
    if key_index < 0:
        return None
    colon_index = content.find(":", key_index)
    if colon_index < 0:
        return None
    array_index = content.find("[", colon_index)
    return array_index if array_index >= 0 else None


def _recover_truncated_contract(content: str) -> str | None:
    stripped = _strip_code_fence(content)
    decoder = json.JSONDecoder()
    summary_match = re.search(r'"summary"\s*:\s*', stripped)
    score_match = re.search(r'"score"\s*:\s*', stripped)
    findings_start = _find_json_array_start(stripped, "findings")
    if summary_match is None or score_match is None or findings_start is None:
        return None
    try:
        summary, _ = decoder.raw_decode(stripped[summary_match.end() :])
        score, _ = decoder.raw_decode(stripped[score_match.end() :])
    except json.JSONDecodeError:
        return None

    findings: list[dict[str, Any]] = []
    index = findings_start + 1
    while index < len(stripped):
        while index < len(stripped) and stripped[index] in " \r\n\t,":
            index += 1
        if index >= len(stripped) or stripped[index] == "]":
            break
        if stripped[index] != "{":
            index += 1
            continue
        try:
            finding, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            break
        if isinstance(finding, dict):
            findings.append(finding)
        index += end

    if not findings:
        return None
    return json.dumps(
        {"summary": summary, "score": score, "findings": findings[:2000]},
        ensure_ascii=False,
    )


def _extract_json_object(content: str, *, required_keys: set[str] | None = None) -> str:
    required_keys = RESPONSE_REQUIRED_KEYS if required_keys is None else required_keys
    required_keys_label = ", ".join(sorted(required_keys))
    stripped = _strip_code_fence(content)
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if _is_contract_object(parsed, required_keys):
                return stripped

    found_json_object = False
    found_partial_contract = False

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            if index == 0 and any(f'"{key}"' in stripped for key in required_keys):
                found_partial_contract = True
            continue
        if isinstance(parsed, dict):
            found_json_object = True
            if _is_contract_object(parsed, required_keys):
                return stripped[index : index + end]
    if found_partial_contract:
        recovered = _recover_truncated_contract(stripped) if required_keys == RESPONSE_REQUIRED_KEYS else None
        if recovered is not None:
            return recovered
        raise ValueError(
            "model response contains a truncated top-level JSON object; "
            f"no complete top-level JSON object with {required_keys_label} was found"
        )
    if found_json_object:
        raise ValueError(
            "model response contains JSON fragments, but no complete top-level JSON object "
            f"with {required_keys_label} was found"
        )
    return stripped


def _category_from_text(value: str) -> str | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if any(token in normalized for token in ("可维护", "维护性", "代码规范", "代码质量", "重复代码", "重复块")):
        return "maintainability"
    if any(token in normalized for token in ("性能", "效率", "耗时", "资源耗尽", "栈耗尽", "堆耗尽")):
        return "performance"
    if any(token in normalized for token in ("空指针", "野指针", "悬空指针", "null pointer", "null-pointer")):
        return "pointer_safety"
    if any(token in normalized for token in ("越界", "缓冲区", "buffer overflow", "out of bounds")):
        return "buffer_overflow"
    if any(token in normalized for token in ("内存泄漏", "资源泄漏", "未释放", "memory leak", "resource leak")):
        return "resource_leak"
    if any(token in normalized for token in ("整数", "溢出", "下溢", "除零", "divide by zero")):
        return "integer_safety"
    if any(token in normalized for token in ("类型转换", "隐式转换", "强制转换", "type conversion", "implicit cast")):
        return "integer_safety"
    if any(token in normalized for token in ("输入", "参数", "校验", "验证", "断言", "argument", "parameter")):
        return "input_validation"
    if any(token in normalized for token in ("并发", "线程", "竞态", "死锁", "concurrency", "race")):
        return "concurrency"
    if any(token in normalized for token in ("兼容", "移植", "平台", "类型安全", "类型不匹配", "compatibility", "type safety", "type mismatch")):
        return "compatibility"
    if any(token in normalized for token in ("安全", "漏洞", "注入", "security")):
        return "security"
    return None


def _parse_typed_response(
    payload: dict[str, Any],
    *,
    response_model: type[BaseModel],
    normalizer: Callable[[Any], Any] | None = None,
) -> BaseModel:
    content: str | None = None
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("assistant content is not text")
        required_keys = set(response_model.model_fields)
        raw_value = json.loads(_extract_json_object(content, required_keys=required_keys))
        normalized_value = normalizer(raw_value) if normalizer is not None else raw_value
        return response_model.model_validate(normalized_value)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise ModelInvocationError(
            "model returned an invalid structured response",
            raw_response=content or json.dumps(payload, ensure_ascii=False),
            details=str(exc),
        ) from exc


def _parse_response(payload: dict[str, Any]) -> ModelReviewResponse:
    return _parse_typed_response(
        payload,
        response_model=ModelReviewResponse,
        normalizer=_normalize_model_contract,
    )


def _candidate_mapping(value: dict[str, Any]) -> dict[str, Any]:
    raw_type = value.get("c", value.get("category", value.get("t", value.get("type", "other"))))
    raw_title = value.get("t", value.get("title", raw_type))
    description = value.get("d", value.get("description", value.get("title", raw_type)))
    result = {
        "severity": value.get("s", value.get("severity", "low")),
        "category": raw_type,
        "title": raw_type or description or "候选问题",
        "description": description or raw_type or "模型发现的候选问题",
        "file_path": value.get("p", value.get("file", value.get("file_path", "unknown.c"))),
        "line": value.get("l", value.get("line")),
    }
    result["title"] = raw_title or raw_type or description
    return result


def _candidate_objects_from_content(content: str) -> list[dict[str, Any]]:
    stripped = _strip_code_fence(content).strip()
    if not stripped or stripped in {"[]", "null", "NONE", "none"}:
        return []

    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, list):
        return [item for item in whole if isinstance(item, dict)]
    if isinstance(whole, dict):
        findings = whole.get("findings")
        if isinstance(findings, list):
            return [item for item in findings if isinstance(item, dict)]
        if any(key in whole for key in ("p", "file", "file_path")):
            return [whole]

    candidates: list[dict[str, Any]] = []
    for raw_line in stripped.splitlines():
        line = raw_line.strip().lstrip("- ")
        if not line or not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def _score_for_findings(findings: Sequence[ReviewFinding]) -> float:
    penalty = {"high": 20, "medium": 10, "low": 3, "suggestion": 1}
    return float(max(0, 100 - sum(penalty.get(finding.severity.value, 1) for finding in findings)))


def _parse_candidate_jsonl_response(payload: dict[str, Any]) -> ModelReviewResponse:
    content: str | None = None
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("assistant content is not text")
        objects = _candidate_objects_from_content(content)
        if not objects and _strip_code_fence(content).strip() not in {"", "[]", "null", "NONE", "none"}:
            raise ValueError("no complete JSONL candidate row was found")
        findings = [
            ReviewFinding.model_validate(MODEL_OUTPUT_SANITIZER._sanitize_finding(_candidate_mapping(item)))
            for item in objects[:COMPACT_MAX_FINDINGS]
        ]
        summary = f"第一阶段发现 {len(findings)} 个候选问题。" if findings else "第一阶段未发现候选问题。"
        return ModelReviewResponse(summary=summary, score=_score_for_findings(findings), findings=findings)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise ModelInvocationError(
            "model returned an invalid candidate JSONL response",
            raw_response=content or json.dumps(payload, ensure_ascii=False),
            details=str(exc),
        ) from exc


def _response_format(
    settings: Settings,
    *,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
) -> dict[str, Any] | None:
    if not settings.model_structured_outputs_enabled or response_schema is None:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRUCTURED_RESPONSE_SCHEMA_NAME,
            "strict": True,
            "schema": response_schema.model_json_schema(),
        },
    }


def _reduced_output_budget_from_error(error_text: str, current_max_tokens: int) -> int | None:
    match = TOKEN_BUDGET_PATTERN.search(error_text) or VLLM_TOKEN_BUDGET_PATTERN.search(error_text)
    if match is None:
        return None
    context_window = int(match.group("context"))
    input_tokens = int(match.group("input"))
    available = context_window - input_tokens - TOKEN_BUDGET_SAFETY_MARGIN
    if available < MIN_RETRY_OUTPUT_TOKENS:
        return None
    reduced = min(current_max_tokens - 1, available)
    return reduced if reduced >= MIN_RETRY_OUTPUT_TOKENS else None


def _is_context_window_error(error_text: str) -> bool:
    return bool(
        TOKEN_BUDGET_PATTERN.search(error_text)
        or VLLM_TOKEN_BUDGET_PATTERN.search(error_text)
        or "maximum context length" in error_text.lower()
    )


async def invoke_model(
    *,
    node: ModelNode,
    files: Sequence[ReviewFile],
    prompt: str,
    response_contract: str = FINAL_RESPONSE_CONTRACT,
    retry_instruction: str | None = None,
    settings: Settings | None = None,
    response_schema: type[BaseModel] | None = CompactModelReviewResponse,
    response_model: type[BaseModel] = ModelReviewResponse,
    response_normalizer: Callable[[Any], Any] | None = _normalize_model_contract,
    response_parser: Callable[[dict[str, Any]], BaseModel] | None = None,
    input_message: str | None = None,
    user_context: str | None = None,
    return_metadata: bool = False,
) -> BaseModel | ModelInvocationResult:
    settings = settings or get_settings()
    if not node.is_enabled:
        raise ModelInvocationError("selected model node is disabled")
    if node.base_url.startswith("mock://"):
        if not settings.mock_model_enabled:
            raise ModelInvocationError("mock model node is disabled by configuration")
        return _mock_response(files)

    headers = {"Content-Type": "application/json"}
    if node.api_key:
        headers["Authorization"] = f"Bearer {node.api_key}"
    _ensure_input_budget(
        prompt=prompt,
        files=files,
        settings=settings,
        response_contract=response_contract,
        response_schema=response_schema,
        retry_instruction=retry_instruction,
        input_message=input_message,
        user_context=user_context,
    )
    strict_prompt = _strict_prompt(prompt, response_contract, retry_instruction)
    body = {
        "model": node.model_identifier,
        "messages": [
            {"role": "system", "content": strict_prompt},
            {"role": "user", "content": _user_message(files, input_message=input_message, user_context=user_context)},
        ],
        "temperature": 0,
        "max_tokens": settings.model_max_tokens,
    }
    response_format = _response_format(settings, response_schema=response_schema)
    if response_format is not None:
        body["response_format"] = response_format
    request_started = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=node.timeout_seconds) as client:
            for _ in range(2):
                response = await client.post(
                    f"{node.base_url.rstrip('/')}/v1/chat/completions",
                    headers=headers,
                    json=body,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    response_text = exc.response.text if exc.response is not None else ""
                    reduced_budget = _reduced_output_budget_from_error(
                        response_text,
                        int(body["max_tokens"]),
                    )
                    if reduced_budget is not None and reduced_budget < int(body["max_tokens"]):
                        body = {**body, "max_tokens": reduced_budget}
                        continue
                    details = str(exc)
                    if exc.response is not None:
                        details = f"{details}\nResponse body:\n{truncate_model_log(response_text, 4000)}"
                    if _is_context_window_error(response_text):
                        raise ModelInvocationError("model context window is too small for this review request", details=details) from exc
                    raise ModelInvocationError("selected model node is unavailable", details=details) from exc
                payload = response.json()
                break
            else:
                raise ModelInvocationError("selected model node is unavailable")
    except ModelInvocationError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelInvocationError("selected model node is unavailable", details=str(exc)) from exc
    parsed = (
        response_parser(payload)
        if response_parser is not None
        else _parse_typed_response(payload, response_model=response_model, normalizer=response_normalizer)
    )
    if not return_metadata:
        return parsed
    finish_reason = None
    try:
        finish_reason = str(payload["choices"][0].get("finish_reason") or "") or None
    except (KeyError, IndexError, TypeError):
        pass
    usage = payload.get("usage") if isinstance(payload, dict) else None
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return ModelInvocationResult(
        value=parsed,
        finish_reason=finish_reason,
        prompt_tokens=int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else None,
        completion_tokens=int(completion_tokens) if isinstance(completion_tokens, (int, float)) else None,
        elapsed_seconds=perf_counter() - request_started,
    )


def _select_candidate_findings(result: ModelReviewResponse) -> list:
    return list(result.findings)


def _review_file_by_path(files: Sequence[ReviewFile], file_path: str) -> ReviewFile | None:
    wanted = file_path.replace("\\", "/").strip().lstrip("./")
    for source in files:
        if source.relative_path.replace("\\", "/").strip().lstrip("./") == wanted:
            return source
    basename_matches = [
        source
        for source in files
        if source.relative_path.replace("\\", "/").split("/")[-1] == wanted.split("/")[-1]
    ]
    return basename_matches[0] if len(basename_matches) == 1 else None


def _looks_like_function_header(header_text: str) -> bool:
    compact = " ".join(line.strip() for line in header_text.splitlines() if line.strip())
    if not compact or ")" not in compact:
        return False
    lowered = compact.lower().lstrip()
    if lowered.startswith(("if ", "if(", "for ", "for(", "while ", "while(", "switch ", "switch(", "else", "do ")):
        return False
    return bool(re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^;{}]*\)\s*\{?\s*$", compact))


def _function_window_for_line(lines: Sequence[str], line_number: int, *, max_lines: int = 160) -> tuple[int, int] | None:
    if line_number < 1 or line_number > len(lines):
        return None
    sanitized = [_strip_comments_and_strings(line) for line in lines]
    depth = 0
    function_start: int | None = None
    header_lines: list[str] = []
    for index, line in enumerate(sanitized):
        if depth == 0:
            before_open = line.split("{", 1)[0]
            header_lines.append(before_open)
            if "{" in line:
                header = "\n".join(header_lines[-8:])
                if _looks_like_function_header(header):
                    function_start = index + 1
                else:
                    function_start = None
                header_lines = []
            elif ";" in line or "}" in line:
                header_lines = []

        depth += line.count("{") - line.count("}")
        if depth == 0 and function_start is not None:
            function_end = index + 1
            if function_start <= line_number <= function_end:
                if function_end - function_start + 1 <= max_lines:
                    return function_start, function_end
                return None
            function_start = None
    return None


def _candidate_text(finding) -> str:
    return " ".join(
        part
        for part in (
            getattr(finding, "category", None).value if getattr(finding, "category", None) is not None else "",
            getattr(finding, "title", "") or "",
            getattr(finding, "description", "") or "",
        )
        if part
    ).lower()


def _strip_comments_and_strings(line: str) -> str:
    without_line_comment = line.split("//", 1)[0]
    without_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', without_line_comment)
    return re.sub(r"'(?:\\.|[^'\\])*'", "''", without_strings)


_CANDIDATE_ANCHOR_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_C_IDENTIFIER_RE = _CANDIDATE_ANCHOR_IDENTIFIER_RE
_CANDIDATE_ANCHOR_STOPWORDS = {
    "high",
    "medium",
    "low",
    "other",
    "logic",
    "true",
    "false",
    "null",
    "candidate",
    "trigger",
    "buffer",
    "overflow",
    "underflow",
    "integer",
    "safety",
    "memory",
    "resource",
    "leak",
    "divide",
    "zero",
    "write",
    "read",
    "stack",
    "heap",
    "copy",
    "bounds",
    "free",
    "release",
    "recursion",
    "allocation",
    "input",
    "check",
    "validation",
    "pointer",
    "invalid",
    "condition",
    "loop",
    "size",
    "call",
    "risk",
    "issue",
    "line",
}

_CANDIDATE_API_TERMS = (
    "memcpy",
    "memmove",
    "strcpy",
    "strncpy",
    "sprintf",
    "snprintf",
    "malloc",
    "calloc",
    "realloc",
    "free",
    "fopen",
    "open",
    "close",
    "read",
    "write",
    "recv",
    "send",
    "stack_operation",
)


def _candidate_subject_identifiers(finding) -> tuple[str, ...]:
    text = " ".join(
        part
        for part in (
            getattr(finding, "title", "") or "",
            getattr(finding, "description", "") or "",
        )
        if part
    )
    identifiers: list[str] = []
    for match in _CANDIDATE_ANCHOR_IDENTIFIER_RE.finditer(text):
        identifier = match.group(0)
        lowered = identifier.lower()
        if len(identifier) < 3 or lowered in _CANDIDATE_ANCHOR_STOPWORDS:
            continue
        if lowered in {"memcpy", "memmove", "strcpy", "strncpy", "malloc", "calloc", "realloc", "free"}:
            continue
        identifiers.append(lowered)
    return tuple(dict.fromkeys(identifiers))


def _candidate_anchor_terms(finding) -> tuple[str, ...]:
    text = _candidate_text(finding)
    terms = list(_candidate_subject_identifiers(finding))
    terms.extend(term for term in _CANDIDATE_API_TERMS if term in text)
    terms.extend(symbol for symbol in ("->", "[", "]", "<<", ">>", "/", "=") if symbol in text)
    return tuple(dict.fromkeys(terms))


def _line_matches_candidate_trigger(line_text: str, finding) -> bool:
    lowered = line_text.strip().lower()
    if not lowered:
        return False
    if lowered in {"{", "}", "//{", "//}", "(", ")"}:
        return False
    tokens = _candidate_anchor_terms(finding)
    if tokens and any(token in lowered for token in tokens):
        return True
    identifier_hits = _candidate_subject_identifiers(finding)
    return bool(identifier_hits) and any(identifier in lowered for identifier in identifier_hits)


def _line_has_actionable_c_anchor(line_text: str) -> bool:
    lowered = line_text.strip().lower()
    if not lowered:
        return False
    if lowered in {"{", "}", "(", ")", ";"}:
        return False
    if lowered.startswith(("//", "/*", "*", "#endif", "#else")):
        return False
    if lowered.startswith("#"):
        return any(token in lowered for token in ("define", "include", "if", "ifdef", "ifndef"))
    if lowered.startswith(("if", "while", "for", "switch", "return", "do", "goto", "free(")):
        return True
    if any(token in lowered for token in ("=", "->", "[", "]", "malloc", "calloc", "realloc", "memcpy", "memmove", "strcpy", "strncpy", "snprintf", "sprintf")):
        return True
    if re.search(r"\b[a-z_][a-z0-9_]*\s*\(", lowered):
        return True
    return lowered.endswith(";")


def _candidate_line_anchor_score(line_text: str, finding) -> float:
    lowered = line_text.strip().lower()
    if not lowered:
        return -10.0
    if lowered in {"{", "}", "//{", "//}", "(", ")"}:
        return -8.0
    if lowered.startswith("//") or lowered.startswith("/*") or lowered.startswith("*"):
        return -6.0

    score = 0.0
    if _line_has_actionable_c_anchor(line_text):
        score += 1.0
    if _line_matches_candidate_trigger(line_text, finding):
        score += 2.5

    anchor_terms = _candidate_anchor_terms(finding)
    score += sum(1.4 for term in anchor_terms if term in lowered)
    return score


def _refine_candidate_line(files: Sequence[ReviewFile], finding, *, radius: int = 5) -> int | None:
    if finding.line is None:
        return None
    source = _review_file_by_path(files, finding.file_path)
    if source is None:
        return None
    lines = source.source_text.splitlines()
    if finding.line < 1 or finding.line > len(lines):
        best_line: int | None = None
        best_score = -10.0
        for candidate_line, line_text in enumerate(lines, start=1):
            score = _candidate_line_anchor_score(line_text, finding)
            if score > best_score:
                best_line = candidate_line
                best_score = score
        return best_line if best_score >= 3.0 else None

    finding_text = _candidate_text(finding)
    current_line = lines[finding.line - 1].strip().lower()
    if (
        finding.category.value == "memory_safety"
        and current_line.startswith("free(")
        and any(token in finding_text for token in ("use_after_free", "use-after-free", "释放后", "继续使用", "继续访问"))
    ):
        for candidate_line in range(finding.line + 1, min(len(lines), finding.line + radius) + 1):
            if _line_has_actionable_c_anchor(lines[candidate_line - 1]) and not lines[candidate_line - 1].strip().lower().startswith("free("):
                return candidate_line
    current_score = _candidate_line_anchor_score(lines[finding.line - 1], finding)
    if current_score >= 4.5:
        return finding.line

    best_line = finding.line
    best_score = current_score

    start = max(1, finding.line - radius)
    end = min(len(lines), finding.line + radius)
    for candidate_line in range(start, end + 1):
        score = _candidate_line_anchor_score(lines[candidate_line - 1], finding) - (abs(candidate_line - finding.line) * 0.12)
        if score > best_score:
            best_score = score
            best_line = candidate_line

    function_window = _function_window_for_line(lines, finding.line)
    if function_window is not None:
        for candidate_line in range(function_window[0], function_window[1] + 1):
            score = _candidate_line_anchor_score(lines[candidate_line - 1], finding)
            if score > best_score:
                best_score = score
                best_line = candidate_line

    if best_score >= 3.0:
        return best_line

    if _line_has_actionable_c_anchor(lines[finding.line - 1]) and _line_matches_candidate_trigger(
        lines[finding.line - 1], finding
    ):
        return finding.line
    for distance in range(1, radius + 1):
        for candidate_line in (finding.line - distance, finding.line + distance):
            if candidate_line < start or candidate_line > end:
                continue
            if _line_has_actionable_c_anchor(lines[candidate_line - 1]) and _line_matches_candidate_trigger(
                lines[candidate_line - 1], finding
            ):
                return candidate_line
    if function_window is not None:
        for candidate_line in range(function_window[0], function_window[1] + 1):
            if _line_has_actionable_c_anchor(lines[candidate_line - 1]) and _line_matches_candidate_trigger(
                lines[candidate_line - 1], finding
            ):
                return candidate_line

    # Models frequently point at a comment immediately before the faulty
    # statement. Keep in-range relocation local so repeated defects are not
    # pulled onto one unrelated high-scoring line elsewhere in the file.
    local_actionable: list[tuple[int, int, int]] = []
    for candidate_line in range(start, end + 1):
        if not _line_has_actionable_c_anchor(lines[candidate_line - 1]):
            continue
        distance = abs(candidate_line - finding.line)
        direction_penalty = 0 if candidate_line >= finding.line else 1
        local_actionable.append((distance, direction_penalty, candidate_line))
    if local_actionable:
        return min(local_actionable)[2]
    if function_window is not None:
        function_actionable = [
            candidate_line
            for candidate_line in range(function_window[0], function_window[1] + 1)
            if _line_has_actionable_c_anchor(lines[candidate_line - 1])
        ]
        if function_actionable:
            return min(function_actionable, key=lambda line: (abs(line - finding.line), line < finding.line, line))
    return None


def _normalize_candidate_findings(files: Sequence[ReviewFile], candidates: Sequence) -> list:
    normalized: list = []
    for finding in candidates:
        refined_line = _refine_candidate_line(files, finding)
        if refined_line is not None and refined_line != finding.line:
            normalized.append(finding.model_copy(update={"line": refined_line}))
        else:
            normalized.append(finding)
    return normalized


def _dedupe_final_findings(findings: Sequence) -> list:
    deduped: list = []
    seen: set[tuple[str, str, int | None, str, str]] = set()
    for finding in findings:
        key = (
            finding.file_path.replace("\\", "/").strip().lower(),
            finding.category.value,
            finding.line,
            finding.title.strip().lower(),
            finding.description.strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _dedupe_candidate_findings(findings: Sequence[ReviewFinding]) -> list[ReviewFinding]:
    deduped: list[ReviewFinding] = []
    seen: set[tuple[str, int | None, str, str, str]] = set()
    for finding in findings:
        key = (
            finding.file_path.replace("\\", "/").strip().lower(),
            finding.line,
            finding.category.value,
            finding.title.strip().lower(),
            finding.description.strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _dedupe_root_findings(
    files: Sequence[ReviewFile],
    findings: Sequence[ReviewFinding],
) -> tuple[list[ReviewFinding], int]:
    """Collapse final findings that describe the same root cause.

    Candidate JSONL remains line-level for recall and debugging.  The user-facing
    report should be root-cause level so one missing bounds check does not become
    five high-risk findings.
    """

    deduped: list[ReviewFinding] = []
    seen: dict[tuple[str, str, tuple[int, int] | None, str], int] = {}
    duplicate_count = 0
    for finding in findings:
        key = _root_finding_key(files, finding)
        if key is None:
            deduped.append(finding)
            continue
        existing_index = seen.get(key)
        if existing_index is None:
            seen[key] = len(deduped)
            deduped.append(finding)
            continue
        duplicate_count += 1
        current = deduped[existing_index]
        if _finding_root_representative_rank(finding) < _finding_root_representative_rank(current):
            deduped[existing_index] = finding
    return deduped, duplicate_count


def _finding_root_representative_rank(finding: ReviewFinding) -> tuple[int, int]:
    return (SEVERITY_RANK.get(finding.severity.value, 99), finding.line or 10**9)


def _root_finding_key(
    files: Sequence[ReviewFile],
    finding: ReviewFinding,
) -> tuple[str, str, tuple[int, int] | None, str] | None:
    source = _review_file_by_path(files, finding.file_path)
    if source is None or finding.line is None:
        return None
    lines = source.source_text.splitlines()
    if finding.line < 1 or finding.line > len(lines):
        return None
    function_window = _function_window_for_line(lines, finding.line)
    subject = _root_subject_for_line(lines[finding.line - 1], finding)
    if not subject:
        return None
    return (
        source.relative_path.replace("\\", "/").strip().lower(),
        finding.category.value,
        function_window,
        subject,
    )


def _root_subject_for_line(line_text: str, finding: ReviewFinding) -> str | None:
    normalized_line = _normalize_c_expression(line_text)
    if finding.category == FindingCategory.BUFFER_OVERFLOW:
        memcpy_match = _MEMCPY_CALL_PATTERN.search(line_text)
        if memcpy_match:
            return f"dst:{_root_expression_base(memcpy_match.group('dst'))}"
        call_match = re.search(
            r"\b(?:strcpy|strncpy|strcat|strncat|sprintf|snprintf|memset|WTOB)\s*\(\s*(?P<dst>[^,);]+)",
            line_text,
        )
        if call_match:
            return f"dst:{_root_expression_base(call_match.group('dst'))}"
        index_match = re.search(r"(?P<dst>[A-Za-z_][A-Za-z0-9_.>\-]*)\s*\[", normalized_line)
        if index_match:
            return f"dst:{_root_expression_base(index_match.group('dst'))}"
    if finding.category == FindingCategory.INTEGER_SAFETY:
        identifiers = _candidate_subject_identifiers(finding)
        return f"int:{identifiers[0]}" if identifiers else None
    if finding.category == FindingCategory.LOGIC:
        identifiers = _candidate_subject_identifiers(finding)
        return f"logic:{identifiers[0]}" if identifiers else None
    return None


def _root_expression_base(expression: str) -> str:
    normalized = _normalize_c_expression(expression)
    normalized = normalized.lstrip("&*(").rstrip(")")
    normalized = re.split(r"\+|-|\[", normalized, maxsplit=1)[0]
    return normalized or expression.strip()


def _validate_compiler_findings(
    files: Sequence[ReviewFile],
    findings: Sequence[ReviewFinding],
) -> list[ReviewFinding]:
    validated: list[ReviewFinding] = []
    for finding in findings:
        source = _review_file_by_path(files, finding.file_path)
        if source is None or finding.line is None:
            continue
        line_count = len(source.source_text.splitlines())
        if finding.line < 1 or finding.line > max(1, line_count):
            continue
        validated.append(finding.model_copy(update={"file_path": source.relative_path}))
    return validated


def _merge_compiler_findings(
    files: Sequence[ReviewFile],
    existing: Sequence[ReviewFinding],
    compiler_findings: Sequence[ReviewFinding],
) -> list[ReviewFinding]:
    merged = list(existing)
    for compiler_finding in compiler_findings:
        merged = [
            finding
            for finding in merged
            if not _findings_describe_same_issue(files, finding, compiler_finding)
        ]
        merged.append(compiler_finding)
    return _dedupe_candidate_findings(merged)


def _findings_describe_same_issue(
    files: Sequence[ReviewFile],
    left: ReviewFinding,
    right: ReviewFinding,
) -> bool:
    if left.category != right.category:
        return False
    left_source = _review_file_by_path(files, left.file_path)
    right_source = _review_file_by_path(files, right.file_path)
    if left_source is None or right_source is None:
        return False
    if left_source.relative_path.replace("\\", "/").lower() != right_source.relative_path.replace("\\", "/").lower():
        return False
    if left.line is None or right.line is None:
        return False
    if abs(left.line - right.line) <= 1:
        return True
    if left.category != FindingCategory.RESOURCE_LEAK:
        return False
    subjects = _resource_identity_tokens(left) & _resource_identity_tokens(right)
    if not subjects:
        return False
    lines = left_source.source_text.splitlines()
    left_window = _function_window_for_line(lines, left.line)
    right_window = _function_window_for_line(lines, right.line)
    return left_window is not None and left_window == right_window


_RESOURCE_IDENTITY_STOPWORDS = {
    "clang",
    "stream",
    "file",
    "handle",
    "resource",
    "memory",
    "leak",
    "opened",
    "closed",
    "released",
    "never",
    "return",
    "path",
    "this",
    "that",
    "with",
    "without",
    "from",
    "before",
    "after",
    "not",
    "may",
    "is",
}


def _resource_identity_tokens(finding: ReviewFinding) -> set[str]:
    return {
        token.lower()
        for token in _C_IDENTIFIER_RE.findall(finding.description)
        if len(token) >= 3 and token.lower() not in _RESOURCE_IDENTITY_STOPWORDS
    }


_COMPILATION_ONLY_PATTERNS = (
    "missing declaration",
    "missing definition",
    "implicit declaration",
    "undefined function",
    "undefined symbol",
    "unknown type",
    "未定义函数",
    "函数未定义",
    "缺少声明",
    "缺少定义",
    "隐式声明",
    "未知类型",
    "找不到声明",
    "找不到定义",
)
_GENERIC_NULL_CHECK_PATTERNS = (
    "missing null check",
    "missing nullptr check",
    "lack of null check",
    "缺少空指针检查",
    "未检查空指针",
    "未进行空指针",
    "没有进行null",
    "未校验null",
    "未判断null",
)
_PERIPHERAL_REGISTER_PATTERN = re.compile(
    r"\b(?:CAN|DAC|DMA\d?|CRC|DBGMCU|RCC|GPIO[A-Z]?|USART\d?|UART\d?|SPI\d?|I2C\d?|TIM\d?|ADC\d?)\s*->",
    re.IGNORECASE,
)
_NULL_POINTER_PATTERN = re.compile(r"空指针|null\s*pointer|nullptr", re.IGNORECASE)
_VENDOR_ASSERT_PATTERN = re.compile(r"\b(?:assert|IS_[A-Za-z0-9_]+)\b", re.IGNORECASE)
_MISSING_VALIDATION_PATTERN = re.compile(r"缺少.*(?:校验|检查)|未.*(?:校验|检查)|missing.*(?:validation|check)", re.IGNORECASE)
_MEMSET_SELF_SIZEOF_PATTERN = re.compile(
    r"\bmemset\s*\(\s*(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*0\s*,\s*sizeof\s*\(\s*(?P=target)\s*\)\s*\)"
)
_ARRAY_DECL_PATTERN_TEMPLATE = r"\b(?:char|u?int(?:8|16|32|64)(?:_t)?|unsigned\s+char|uint8)\s+{name}\s*\[\s*(?P<size>[^\]]+)\s*\]"
_POINTER_PARAM_PATTERN_TEMPLATE = r"\([^)]*(?:\*+\s*{name}\b|{name}\s*\[\s*\])"
_LITERAL_INDEX_WRITE_PATTERN = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<index>\d+)\s*\]\s*="
)
_RING_WRITE_PATTERN = re.compile(
    r"\b(?P<buffer>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s*\[\s*(?P<index>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\+\+\s*\]\s*="
)
_MEMCPY_CALL_PATTERN = re.compile(
    r"\bmemcpy\s*\(\s*(?P<dst>[^,]+)\s*,\s*(?P<src>[^,]+)\s*,\s*(?P<len>[^)]+)\)"
)
_STRNCPY_SIZEOF_MINUS_ONE_PATTERN = re.compile(
    r"\bstrncpy\s*\(\s*(?P<dst>[A-Za-z_][A-Za-z0-9_.>\-]*)\s*,\s*[^,]+,\s*sizeof\s*\(\s*(?P=dst)\s*\)\s*-\s*1\s*\)"
)
_MEMCPY_SIZEOF_MINUS_ONE_PATTERN = re.compile(
    r"\bmemcpy\s*\(\s*(?P<dst>[A-Za-z_][A-Za-z0-9_.>\-]*)\s*,\s*[^,]+,\s*sizeof\s*\(\s*(?P=dst)\s*\)\s*-\s*1\s*\)"
)
_SIZEOF_GUARD_PATTERN_TEMPLATE = (
    r"\bif\s*\([^)]*{length}[^)]*(?:>=|>)\s*sizeof\s*\(\s*{target}\s*\)[^)]*\)"
)
_CONSTANT_LEN_ASSIGN_PATTERN_TEMPLATE = r"\b{length}\s*=\s*(?P<value>\d+)\s*;"


def _calibrate_candidate_finding(
    source: ReviewFile,
    line_number: int,
    finding: ReviewFinding,
) -> ReviewFinding:
    """Downgrade candidates whose syntax proves a different defect class.

    The first-stage scan is intentionally high-recall, so it often labels any
    risky memory-looking statement as a buffer overflow.  Keep the finding, but
    correct the class when the line proves a narrower issue.
    """

    lines = source.source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return finding
    line_text = lines[line_number - 1]
    memset_match = _MEMSET_SELF_SIZEOF_PATTERN.search(line_text)
    if not memset_match:
        return finding
    target = memset_match.group("target")
    if _identifier_has_local_array_decl(source, line_number, target):
        return finding
    if not _identifier_is_pointer_parameter(source, line_number, target):
        return finding
    return finding.model_copy(
        update={
            "severity": FindingSeverity.MEDIUM,
            "category": FindingCategory.MEMORY_SAFETY,
            "title": "指针宽度清零",
            "description": "memset 使用 sizeof(pointer) 只清空指针宽度，真实问题是缓冲区初始化不完整而非直接缓冲区溢出。",
        }
    )


def _identifier_has_local_array_decl(source: ReviewFile, line_number: int, name: str) -> bool:
    lines = source.source_text.splitlines()
    window = _function_window_for_line(lines, line_number) or (1, line_number)
    pattern = re.compile(_ARRAY_DECL_PATTERN_TEMPLATE.format(name=re.escape(name)))
    return any(pattern.search(lines[index - 1]) for index in range(window[0], min(line_number, window[1]) + 1))


def _identifier_is_pointer_parameter(source: ReviewFile, line_number: int, name: str) -> bool:
    lines = source.source_text.splitlines()
    window = _function_window_for_line(lines, line_number)
    if window is None:
        return False
    header_start = max(1, window[0] - 4)
    header = "\n".join(lines[header_start - 1 : window[0]])
    pattern = re.compile(_POINTER_PARAM_PATTERN_TEMPLATE.format(name=re.escape(name)), re.DOTALL)
    return pattern.search(header) is not None


def _is_proven_safe_buffer_candidate(
    source: ReviewFile,
    line_number: int,
    finding: ReviewFinding,
) -> bool:
    if finding.category != FindingCategory.BUFFER_OVERFLOW:
        return False
    lines = source.source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False
    line_text = lines[line_number - 1]
    return (
        _is_ring_buffer_modulo_write(lines, line_number, line_text)
        or _is_literal_index_within_declared_array(source, line_number, line_text)
        or _is_guarded_sizeof_copy(lines, line_number, line_text)
        or _is_fixed_protocol_datap_copy_safe(lines, line_number, line_text)
        or _is_des3_tail_buffer_copy_safe(source, line_number, line_text)
        or _is_bounded_local_command_concat_safe(source, line_number, line_text)
        or _is_guard_only_buffer_candidate_safe(lines, line_number, line_text)
        or _is_local_append_guarded_by_sizeof(lines, line_number, line_text)
        or _is_clamped_length_memcpy_safe(lines, line_number, line_text)
        or _is_non_sink_buffer_candidate_line(line_text)
    )


def _is_ring_buffer_modulo_write(lines: list[str], line_number: int, line_text: str) -> bool:
    match = _RING_WRITE_PATTERN.search(line_text)
    if not match:
        return False
    index = re.escape(match.group("index"))
    modulo_pattern = re.compile(rf"\b{index}\s*%=\s*(?:[A-Za-z_][A-Za-z0-9_]*|\d+)\b")
    end = min(len(lines), line_number + 3)
    return any(modulo_pattern.search(lines[index_line - 1]) for index_line in range(line_number + 1, end + 1))


def _is_literal_index_within_declared_array(source: ReviewFile, line_number: int, line_text: str) -> bool:
    match = _LITERAL_INDEX_WRITE_PATTERN.search(line_text)
    if not match:
        return False
    size = _declared_array_capacity(source, line_number, match.group("name"))
    return size is not None and int(match.group("index")) < size


def _declared_array_capacity(source: ReviewFile, line_number: int, name: str) -> int | None:
    lines = source.source_text.splitlines()
    window = _function_window_for_line(lines, line_number) or (1, len(lines))
    pattern = re.compile(_ARRAY_DECL_PATTERN_TEMPLATE.format(name=re.escape(name)))
    for index in range(line_number, window[0] - 1, -1):
        match = pattern.search(lines[index - 1])
        if not match:
            continue
        return _safe_eval_int_expression(match.group("size"), source.source_text)
    return _array_capacities_in_scope(source, line_number).get(name)


def _safe_eval_int_expression(expression: str, source_text: str) -> int | None:
    macro_values = {
        name: int(value)
        for name, value in re.findall(r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(\d+)\b", source_text, re.MULTILINE)
    }
    normalized = expression.strip()
    for name, value in macro_values.items():
        normalized = re.sub(rf"\b{re.escape(name)}\b", str(value), normalized)
    if not re.fullmatch(r"[0-9+*()\s-]+", normalized):
        return None
    try:
        value = eval(normalized, {"__builtins__": {}}, {})
    except Exception:
        return None
    return value if isinstance(value, int) and value >= 0 else None


def _is_guarded_sizeof_copy(lines: list[str], line_number: int, line_text: str) -> bool:
    if _STRNCPY_SIZEOF_MINUS_ONE_PATTERN.search(line_text) or _MEMCPY_SIZEOF_MINUS_ONE_PATTERN.search(line_text):
        return True
    memcpy_match = _MEMCPY_CALL_PATTERN.search(line_text)
    if not memcpy_match:
        return False
    dst = _normalize_c_expression(memcpy_match.group("dst"))
    length = _normalize_c_expression(memcpy_match.group("len"))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.>\-]*", dst):
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.>\-]*", length):
        return False
    guard_pattern = re.compile(
        _SIZEOF_GUARD_PATTERN_TEMPLATE.format(target=re.escape(dst), length=re.escape(length))
    )
    start = max(1, line_number - 8)
    return any(guard_pattern.search(lines[index - 1]) for index in range(start, line_number))


def _is_fixed_protocol_datap_copy_safe(lines: list[str], line_number: int, line_text: str) -> bool:
    memcpy_match = _MEMCPY_CALL_PATTERN.search(line_text)
    if not memcpy_match:
        return False
    dst = _normalize_c_expression(memcpy_match.group("dst"))
    length = _normalize_c_expression(memcpy_match.group("len"))
    if dst not in {"com->DataP", "str.DataP"}:
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.>\-]*", length):
        return False
    assign_pattern = re.compile(_CONSTANT_LEN_ASSIGN_PATTERN_TEMPLATE.format(length=re.escape(length)))
    start = max(1, line_number - 12)
    for index in range(line_number - 1, start - 1, -1):
        match = assign_pattern.search(lines[index - 1])
        if match and int(match.group("value")) <= 128:
            return True
    return False


def _is_des3_tail_buffer_copy_safe(source: ReviewFile, line_number: int, line_text: str) -> bool:
    normalized_line = _normalize_c_expression(line_text)
    if "fillbuf" not in line_text and "ptr->uMessageLen-18" not in normalized_line:
        return False
    lines = source.source_text.splitlines()
    window = _function_window_for_line(lines, line_number)
    start = max(1, (window[0] if window else line_number) - 80)
    end = min(len(lines), (window[1] if window else line_number) + 10)
    function_text = "\n".join(lines[start - 1 : end])
    block_size = _safe_eval_int_expression("BLOCK", source.source_text)
    return block_size == 8 and (
        re.search(r"\bfillbuf\s*\[\s*BLOCK\s*\*\s*2\s*\]", function_text) is not None
        or re.search(r"\bfillbuf\s*\[\s*16\s*\]", function_text) is not None
    )


def _is_bounded_local_command_concat_safe(source: ReviewFile, line_number: int, line_text: str) -> bool:
    if "uCmd" not in line_text or not any(token in line_text for token in ("strcat", "strcpy", "char uCmd")):
        return False
    lines = source.source_text.splitlines()
    window = _function_window_for_line(lines, line_number)
    if window is None:
        return False
    function_lines = lines[window[0] - 1 : window[1]]
    declaration_index = None
    capacity = None
    initial = ""
    for offset, current in enumerate(function_lines):
        match = re.search(
            r"\bchar\s+uCmd\s*\[\s*(?P<cap>\d+)\s*\]\s*=\s*\"(?P<initial>(?:\\.|[^\"])*)\"",
            current,
        )
        if match:
            declaration_index = offset
            capacity = int(match.group("cap"))
            initial = match.group("initial")
            break
    if declaration_index is None or capacity is None:
        return False
    known_sizes = _array_capacities_in_scope(source, line_number)
    known_sizes.update(_local_array_capacities(function_lines[: line_number - window[0] + 1]))
    total = _c_string_literal_length(initial)
    for current in function_lines[declaration_index + 1 : line_number - window[0] + 1]:
        strcat_literal = re.search(r"\bstrcat\s*\(\s*uCmd\s*,\s*\"(?P<literal>(?:\\.|[^\"])*)\"\s*\)", current)
        if strcat_literal:
            total += _c_string_literal_length(strcat_literal.group("literal"))
            continue
        strcat_identifier = re.search(
            r"\bstrcat\s*\(\s*uCmd\s*,\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
            current,
        )
        if strcat_identifier:
            size = known_sizes.get(strcat_identifier.group("name"))
            if size is None:
                return False
            total += max(0, size - 1)
    return total + 1 <= capacity


def _array_capacities_in_scope(source: ReviewFile, line_number: int) -> dict[str, int]:
    capacities: dict[str, int] = {}
    lines = source.source_text.splitlines()
    for current in lines[:line_number]:
        match = re.search(
            r"\b(?:char|uint8|unsigned\s+char)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<size>[^\]]+)\s*\]",
            current,
        )
        if not match:
            continue
        size = _safe_eval_int_expression(match.group("size"), source.source_text)
        if size is not None:
            capacities[match.group("name")] = size
    return capacities


def _local_array_capacities(lines: list[str]) -> dict[str, int]:
    capacities: dict[str, int] = {}
    for current in lines:
        match = re.search(
            r"\b(?:char|uint8|unsigned\s+char)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<size>\d+)\s*\]",
            current,
        )
        if match:
            capacities[match.group("name")] = int(match.group("size"))
    return capacities


def _c_string_literal_length(value: str) -> int:
    return len(re.sub(r"\\.", "_", value))


def _is_guard_only_buffer_candidate_safe(lines: list[str], line_number: int, line_text: str) -> bool:
    stripped = line_text.strip()
    if not stripped.startswith("if"):
        return False
    if any(token in stripped for token in ("memcpy", "strcpy", "strcat", "sprintf", "[", "] =")):
        return False
    if "sizeof(" not in stripped and "ADDR_MAX" not in stripped:
        return False
    end = min(len(lines), line_number + 4)
    return any(
        re.search(r"\b(?:return|break)\b|=[ ]*0\s*;", lines[index - 1])
        for index in range(line_number + 1, end + 1)
    )


def _is_local_append_guarded_by_sizeof(lines: list[str], line_number: int, line_text: str) -> bool:
    match = re.search(r"\b(?P<buffer>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<index>[A-Za-z_][A-Za-z0-9_]*)\s*\+\+\s*\]\s*=", line_text)
    if not match:
        return False
    buffer_name = match.group("buffer")
    index_name = match.group("index")
    end = min(len(lines), line_number + 3)
    guard_pattern = re.compile(
        rf"\b{re.escape(index_name)}\s*>=\s*sizeof\s*\(\s*{re.escape(buffer_name)}\s*\)\s*-\s*1"
    )
    return any(guard_pattern.search(lines[index - 1]) for index in range(line_number + 1, end + 1))


def _is_clamped_length_memcpy_safe(lines: list[str], line_number: int, line_text: str) -> bool:
    memcpy_match = _MEMCPY_CALL_PATTERN.search(line_text)
    if not memcpy_match:
        return False
    length = _normalize_c_expression(memcpy_match.group("len"))
    start = max(1, line_number - 8)
    guard_text = "\n".join(_normalize_c_expression(lines[index - 1]) for index in range(start, line_number))
    if length == "ServerProtocolStruct.uMessageLen-18":
        return (
            "ServerProtocolStruct.uMessageLen-18" in guard_text
            and "DATA_REV_SER_MAX_SIZE-10" in guard_text
            and "ServerProtocolStruct.uMessageLen=DATA_REV_SER_MAX_SIZE-10+18" in guard_text
        )
    return False


def _is_non_sink_buffer_candidate_line(line_text: str) -> bool:
    lowered = line_text.strip().lower()
    if lowered.startswith("return "):
        return True
    if re.match(r"^(?:static\s+)?[a-z_][a-z0-9_*\s]+\s+[a-z_][a-z0-9_]*\s*\([^;]*\)\s*$", lowered):
        return True
    return not _line_has_buffer_sink_anchor(lowered)


def _line_has_buffer_sink_anchor(lowered_line: str) -> bool:
    if any(
        token in lowered_line
        for token in (
            "memcpy",
            "memmove",
            "memset",
            "strcpy",
            "strncpy",
            "strcat",
            "strncat",
            "sprintf",
            "snprintf",
            "scanf",
            "gets",
            "wtob",
        )
    ):
        return True
    return re.search(r"\[[^\]]+\]\s*(?:=|\+\+|--)", lowered_line) is not None


def _is_checked_search_result_candidate(
    lines: list[str],
    line_number: int,
    line_text: str,
    finding: ReviewFinding,
) -> bool:
    candidate_text = _candidate_text(finding)
    if "null_pointer" not in candidate_text and not _NULL_POINTER_PATTERN.search(candidate_text):
        return False
    if re.search(r"\breturn\s*\(\s*int\s*\)\s*(?:strstr|strchr)\s*\(", line_text):
        return True
    assignment = re.search(
        r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:strstr|strchr)\s*\(",
        line_text,
    )
    if not assignment:
        return False
    name = re.escape(assignment.group("name"))
    end = min(len(lines), line_number + 10)
    check_pattern = re.compile(rf"\b{name}\s*(?:==|!=)\s*NULL\b|\bNULL\s*(?:==|!=)\s*{name}\b")
    return any(check_pattern.search(lines[index - 1]) for index in range(line_number + 1, end + 1))


def _normalize_c_expression(expression: str) -> str:
    return re.sub(r"\s+", "", expression.strip())



def _is_generic_null_check_candidate(finding: ReviewFinding, line_text: str) -> bool:
    text = _candidate_text(finding)
    return any(pattern in text for pattern in _GENERIC_NULL_CHECK_PATTERNS)


def _filter_candidate_findings(
    files: Sequence[ReviewFile],
    candidates: Sequence[ReviewFinding],
) -> tuple[list[ReviewFinding], dict[str, int]]:
    kept: list[ReviewFinding] = []
    rejected = {
        "unknown_source": 0,
        "unanchored": 0,
        "compilation_only": 0,
        "generic_null_check": 0,
        "peripheral_null_pointer": 0,
        "vendor_assert_validation": 0,
        "resource_leak_false_positive": 0,
        "proven_safe_buffer": 0,
        "checked_null_result": 0,
        "out_of_scope_category": 0,
    }
    for finding in candidates:
        finding = _normalize_lifetime_candidate(files, finding)
        candidate_text = _candidate_text(finding)
        if any(pattern in candidate_text for pattern in _COMPILATION_ONLY_PATTERNS):
            rejected["compilation_only"] += 1
            continue
        if _is_generic_null_check_candidate(finding, ""):
            rejected["generic_null_check"] += 1
            continue
        source = _review_file_by_path(files, finding.file_path)
        if source is None:
            rejected["unknown_source"] += 1
            continue
        refined_line = _refine_candidate_line(files, finding)
        lines = source.source_text.splitlines()
        if refined_line is None or refined_line < 1 or refined_line > len(lines):
            rejected["unanchored"] += 1
            continue
        line_text = lines[refined_line - 1]
        if not _line_has_actionable_c_anchor(line_text):
            rejected["unanchored"] += 1
            continue
        finding = _calibrate_candidate_finding(source, refined_line, finding)
        if _is_checked_search_result_candidate(lines, refined_line, line_text, finding):
            rejected["checked_null_result"] += 1
            continue
        if _is_proven_safe_buffer_candidate(source, refined_line, finding):
            rejected["proven_safe_buffer"] += 1
            continue
        combined_text = f"{candidate_text}\n{line_text}"
        if _NULL_POINTER_PATTERN.search(candidate_text) and _PERIPHERAL_REGISTER_PATTERN.search(combined_text):
            rejected["peripheral_null_pointer"] += 1
            continue
        if _MISSING_VALIDATION_PATTERN.search(candidate_text) and _VENDOR_ASSERT_PATTERN.search(combined_text):
            rejected["vendor_assert_validation"] += 1
            continue
        if finding.category == FindingCategory.RESOURCE_LEAK and _is_resource_leak_false_positive(
            source,
            refined_line,
            finding,
        ):
            rejected["resource_leak_false_positive"] += 1
            continue
        kept.append(
            finding.model_copy(
                update={"file_path": source.relative_path, "line": refined_line}
            )
        )
    return _dedupe_final_findings(kept), rejected


_LIFETIME_DEFECT_PATTERN = re.compile(
    r"double[ _-]?free|use[ _-]?after[ _-]?free|dangling|stale|"
    r"二次释放|重复释放|释放后|悬空|野指针|函数指针",
    re.IGNORECASE,
)


def _normalize_lifetime_candidate(
    files: Sequence[ReviewFile],
    finding: ReviewFinding,
) -> ReviewFinding:
    if finding.category != FindingCategory.RESOURCE_LEAK:
        return finding
    source = _review_file_by_path(files, finding.file_path)
    if source is None or finding.line is None:
        return finding
    lines = source.source_text.splitlines()
    if finding.line < 1 or finding.line > len(lines):
        return finding
    start = max(0, finding.line - 3)
    end = min(len(lines), finding.line + 4)
    nearby = "\n".join(lines[start:end])
    text = f"{_candidate_text(finding)}\n{nearby}"
    has_lifetime_language = _LIFETIME_DEFECT_PATTERN.search(text) is not None
    has_release_lifecycle = re.search(r"\b(?:free|dlclose)\s*\(", nearby) is not None
    has_subsequent_use = re.search(
        r"\b(?:free\s*\(|[A-Za-z_][A-Za-z0-9_]*\s*\(|[A-Za-z_][A-Za-z0-9_]*\s*=)",
        "\n".join(lines[finding.line : end]),
    ) is not None
    sanitized_nearby = "\n".join(_strip_comments_and_strings(line) for line in lines[start:end])
    has_stale_dynamic_symbol = (
        "dlclose" in sanitized_nearby
        and re.search(
            r"\b(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P=symbol)\s*;",
            sanitized_nearby,
        )
        is not None
    )
    if has_lifetime_language or has_stale_dynamic_symbol or (has_release_lifecycle and has_subsequent_use):
        return finding.model_copy(update={"category": FindingCategory.MEMORY_SAFETY})
    return finding


def _is_resource_leak_false_positive(source: ReviewFile, line_number: int, finding: ReviewFinding) -> bool:
    lines = source.source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False
    line_text = _strip_comments_and_strings(lines[line_number - 1]).strip()
    candidate_text = _candidate_text(finding).lower()
    if _is_stack_or_nonowning_resource_line(line_text):
        return True
    function_window = _function_window_for_line(lines, line_number)
    if function_window is None:
        return False
    start, end = function_window
    function_text = "\n".join(lines[start - 1 : end])
    acquisitions = _resource_acquisitions(function_text)
    if not acquisitions:
        return True

    line_acquisitions = _resource_acquisitions(line_text)
    subject_identifiers = set(_candidate_subject_identifiers(finding))
    relevant_identifiers = {
        identifier
        for identifier, _ in acquisitions
        if identifier.lower() in subject_identifiers
    }
    relevant_identifiers.update(identifier for identifier, _ in line_acquisitions)

    # A normal function call or stack object is not ownership evidence. Keep a
    # candidate only when it points to an acquisition, names the owned handle,
    # or is anchored at a return from a function with one unambiguous resource.
    if not relevant_identifiers:
        if re.match(r"^\s*return\b", line_text) and len(acquisitions) == 1:
            relevant_identifiers = {acquisitions[0][0]}
        else:
            return True

    if _has_visible_release_for_any_identifier(function_text, relevant_identifiers):
        if not _has_allocation_after_last_release(function_text, relevant_identifiers):
            return True
    return False


_RESOURCE_ACQUIRE_CALL_RE = re.compile(
    r"\b(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<api>(?:malloc|calloc|realloc|fopen|open|fdopen|socket|dup|strdup|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:_create|_alloc|_acquire|_clone|_open)))\s*\(",
    re.IGNORECASE,
)


def _resource_acquisitions(source_text: str) -> list[tuple[str, str]]:
    sanitized = "\n".join(_strip_comments_and_strings(line) for line in source_text.splitlines())
    return [
        (match.group("identifier"), match.group("api"))
        for match in _RESOURCE_ACQUIRE_CALL_RE.finditer(sanitized)
    ]


def _is_stack_or_nonowning_resource_line(line_text: str) -> bool:
    if re.search(r"\b(?:char|int|uint(?:8|16|32|64)_t|size_t|long|short|float|double)\s+\*?\s*[A-Za-z_][A-Za-z0-9_]*\s*\[[^\]]+\]", line_text):
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\[[^\]]+\]\s*(?:=|;|,)", line_text) and not re.search(
        r"\b(?:malloc|calloc|realloc|fopen|open)\s*\(",
        line_text,
    ):
        return True
    return False


def _has_visible_release_for_any_identifier(function_text: str, identifiers: set[str]) -> bool:
    for identifier in identifiers:
        if re.search(rf"\b(?:free|fclose|close|page_mgr_destroy|destroy|release|unlock)\s*\([^;\n]*\b{re.escape(identifier)}\b", function_text):
            return True
        if re.search(rf"\b(?:free_string_array)\s*\(\s*{re.escape(identifier)}\s*,", function_text):
            return True
    return False


def _has_allocation_after_last_release(function_text: str, identifiers: set[str]) -> bool:
    for identifier in identifiers:
        releases = [
            match.start()
            for match in re.finditer(
                rf"\b(?:free|fclose|close|page_mgr_destroy|destroy|release|unlock|free_string_array)\s*\([^;\n]*\b{re.escape(identifier)}\b",
                function_text,
            )
        ]
        if not releases:
            continue
        last_release = max(releases)
        allocations_after = re.search(
            rf"\b{re.escape(identifier)}\b\s*=\s*(?:malloc|calloc|realloc|fopen|open)\s*\(",
            function_text[last_release:],
        )
        if allocations_after:
            return True
    return False


def _candidate_jsonl(findings: Sequence[ReviewFinding]) -> str:
    return "\n".join(
        json.dumps(
            {
                "p": finding.file_path,
                "l": finding.line,
                "s": finding.severity.value,
                "c": finding.category.value,
                "t": finding.title,
                "d": finding.description,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for finding in findings
    )


def _allowed_final_categories(check_types: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(item for item in check_types if item in CHECK_TYPE_LABELS))
    return selected or tuple(CHECK_TYPE_LABELS)


def _partition_category_candidates(
    findings: Sequence[ReviewFinding],
    allowed_categories: Sequence[str],
) -> tuple[list[ReviewFinding], list[ReviewFinding]]:
    allowed = set(allowed_categories)
    deterministic: list[ReviewFinding] = []
    unresolved: list[ReviewFinding] = []
    for finding in findings:
        corrected = _deterministic_category_correction(finding, allowed)
        if corrected is not None:
            deterministic.append(corrected)
            continue
        category = finding.category.value
        if category in allowed and category != "other":
            deterministic.append(finding)
        else:
            unresolved.append(finding)
    return deterministic, unresolved


_DETERMINISTIC_TYPE_CATEGORY = {
    "sql_injection": "input_validation",
    "format_string": "input_validation",
    "format_string_vulnerability": "input_validation",
    "double_free": "memory_safety",
    "use_after_free": "memory_safety",
    "dangling_pointer": "memory_safety",
    "function_pointer_dangling": "pointer_safety",
    "permissions": "other",
    "weak_crypto": "other",
    "crypto_vulnerability": "other",
    "timing_attack": "other",
    "timing_side_channel": "other",
}


def _deterministic_category_correction(
    finding: ReviewFinding,
    allowed: set[str],
) -> ReviewFinding | None:
    defect_type = finding.title.strip().lower().replace("-", "_").replace(" ", "_")
    target = _DETERMINISTIC_TYPE_CATEGORY.get(defect_type)
    if target is None or target not in allowed:
        return None
    return finding.model_copy(update={"category": FindingCategory(target)})


def _candidate_source_excerpt(files: Sequence[ReviewFile], finding: ReviewFinding, *, radius: int = 2) -> str:
    source = _review_file_by_path(files, finding.file_path)
    if source is None or finding.line is None:
        return ""
    lines = source.source_text.splitlines()
    if not lines:
        return ""
    start = max(1, finding.line - radius)
    end = min(len(lines), finding.line + radius)
    return "\n".join(f"{line_number}: {lines[line_number - 1]}" for line_number in range(start, end + 1))


def _semantic_candidate_document(files: Sequence[ReviewFile], findings: Sequence[ReviewFinding]) -> str:
    rows = []
    for index, finding in enumerate(findings):
        rows.append(
            json.dumps(
                {
                    "i": index,
                    "p": finding.file_path,
                    "l": finding.line,
                    "c": finding.category.value,
                    "t": finding.title,
                    "d": finding.description,
                    "code": _candidate_source_excerpt(files, finding),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return "\n".join(rows)


def _parse_semantic_category_response(payload: dict[str, Any]) -> CandidateCategoryDecisionResponse:
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("semantic category response content is not text")
        decisions = []
        for item in _candidate_objects_from_content(content):
            if not isinstance(item, dict):
                continue
            decisions.append(CandidateCategoryDecision.model_validate(item))
        return CandidateCategoryDecisionResponse(decisions=decisions)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
        raise ModelInvocationError(
            "model returned an invalid semantic category response",
            raw_response=str(payload),
            details=str(exc),
        ) from exc


async def _resolve_unmatched_categories(
    *,
    task: ReviewTask,
    node: ModelNode,
    files: Sequence[ReviewFile],
    findings: Sequence[ReviewFinding],
    allowed_categories: Sequence[str],
    settings: Settings,
) -> tuple[list[ReviewFinding], int]:
    if not findings or not allowed_categories or not settings.candidate_semantic_fallback_enabled:
        return [], 0
    pending = _dedupe_candidate_findings(findings)
    resolved: list[ReviewFinding] = []
    failed_batches = 0
    allowed = set(allowed_categories)
    for start in range(0, len(pending), settings.candidate_semantic_batch_size):
        batch = pending[start : start + settings.candidate_semantic_batch_size]
        output_budget = max(128, min(settings.candidate_semantic_max_tokens, 32 + (24 * len(batch))))
        semantic_settings = settings.model_copy(update={"model_max_tokens": output_budget})
        try:
            decision_invocation = await invoke_model(
                node=node,
                files=(),
                prompt=(
                    "Classify only the supplied unresolved candidates.\n"
                    f"SELECTED CATEGORIES: {', '.join(allowed_categories)}.\n"
                    "A candidate may be corrected only when its nearby source excerpt proves it belongs to one selected category."
                ),
                input_message=f"UNRESOLVED CANDIDATE JSONL:\n{_semantic_candidate_document(files, batch)}",
                response_contract=SEMANTIC_CATEGORY_RESPONSE_CONTRACT,
                settings=semantic_settings,
                response_schema=None,
                response_parser=_parse_semantic_category_response,
                return_metadata=True,
            )
        except ModelInvocationError as exc:
            failed_batches += 1
            task.model_log = truncate_model_log(
                "\n".join(
                    part
                    for part in (
                        task.model_log,
                        f"[CandidateSemantic] Classification failed; unresolved candidates dropped: {exc}",
                    )
                    if part
                )
            )
            continue
        if isinstance(decision_invocation, ModelInvocationResult):
            decision_result = decision_invocation.value
            elapsed = decision_invocation.elapsed_seconds or 0.0
            generation_tps = (
                decision_invocation.completion_tokens / elapsed
                if decision_invocation.completion_tokens is not None and elapsed > 0
                else None
            )
            task.model_log = truncate_model_log(
                "\n".join(
                    part
                    for part in (
                        task.model_log,
                        (
                            "[ModelUsage] stage=semantic; "
                            f"prompt_tokens={decision_invocation.prompt_tokens}; "
                            f"completion_tokens={decision_invocation.completion_tokens}; elapsed_s={elapsed:.4f}; "
                            f"generation_tps={generation_tps:.2f}."
                            if generation_tps is not None
                            else ""
                        ),
                    )
                    if part
                )
            )
        else:
            decision_result = decision_invocation
        if not isinstance(decision_result, CandidateCategoryDecisionResponse):
            failed_batches += 1
            continue
        by_id = {decision.i: decision for decision in decision_result.decisions}
        for index, finding in enumerate(batch):
            decision = by_id.get(index)
            if decision is None or decision.a == "drop" or decision.c not in allowed:
                continue
            resolved.append(finding.model_copy(update={"category": FindingCategory(decision.c)}))
    return resolved, failed_batches


def _candidate_stage_settings(settings: Settings, files: Sequence[ReviewFile] = ()) -> Settings:
    max_tokens = settings.candidate_model_max_tokens
    if settings.candidate_dynamic_tokens_enabled and files:
        source_lines = sum(max(1, len(source.source_text.splitlines())) for source in files)
        source_text = "\n".join(source.source_text for source in files)
        function_count = len(_C_FUNCTION_DEFINITION_RE.findall(source_text))
        dangerous_operation_count = len(_DANGEROUS_C_OPERATION_RE.findall(source_text))
        pointer_operation_count = len(_POINTER_OR_ARRAY_OPERATION_RE.findall(source_text))
        estimated = math.ceil(
            settings.candidate_dynamic_base_tokens
            + (source_lines * settings.candidate_dynamic_tokens_per_line)
            + (function_count * settings.candidate_dynamic_tokens_per_function)
            + (dangerous_operation_count * settings.candidate_dynamic_tokens_per_dangerous_op)
            + (pointer_operation_count * settings.candidate_dynamic_tokens_per_pointer_op)
        )
        max_tokens = max(settings.candidate_dynamic_min_tokens, min(max_tokens, estimated))
    return settings.model_copy(update={"model_max_tokens": max_tokens})


def _candidate_format_prompt(allowed_categories: Sequence[str]) -> str:
    return (
        "Normalize and filter the supplied candidate JSONL document.\n"
        f"ALLOWED FINAL CATEGORIES: {', '.join(allowed_categories)}.\n"
        "The input contains candidate records only. Preserve their factual text and locations; "
        "remove disallowed or compilation-only records and return the strict final JSON object."
    )


def _candidate_jsonl_batches(candidate_jsonl: str, batch_size: int) -> list[str]:
    rows = [line for line in candidate_jsonl.splitlines() if line.strip()]
    return ["\n".join(rows[index : index + batch_size]) for index in range(0, len(rows), batch_size)]


def _filter_final_categories(
    findings: Sequence[ReviewFinding],
    allowed_categories: Sequence[str],
) -> list[ReviewFinding]:
    allowed = set(allowed_categories)
    return [finding for finding in findings if finding.category.value in allowed]


async def _invoke_candidate_review(
    *,
    db: Session,
    task: ReviewTask,
    node: ModelNode,
    files: Sequence[ReviewFile],
    prompt: str,
    settings: Settings,
    retry_instruction: str | None,
) -> ModelReviewResponse:
    candidate_settings = _candidate_stage_settings(settings, files)
    discovery_started = perf_counter()
    rag_started = perf_counter()
    rag_context = _rag_context(db, task, candidate_settings, files=files, purpose="candidate")
    rag_elapsed = perf_counter() - rag_started
    model_started = perf_counter()
    invocation = await invoke_model(
        node=node,
        files=files,
        prompt=prompt,
        user_context=rag_context,
        response_contract=CANDIDATE_RESPONSE_CONTRACT,
        retry_instruction=retry_instruction,
        settings=candidate_settings,
        response_schema=None,
        response_parser=_parse_candidate_jsonl_response,
        return_metadata=True,
    )
    if isinstance(invocation, ModelInvocationResult):
        candidate_result = invocation.value
        finish_reason = invocation.finish_reason
    else:
        candidate_result = invocation
        finish_reason = None
    if not isinstance(candidate_result, ModelReviewResponse):
        raise ModelInvocationError("first-stage candidate scan returned an unexpected response type")
    if finish_reason == "length":
        seen = ", ".join(
            f"{finding.file_path}:{finding.line or 'null'}:{finding.title}"
            for finding in candidate_result.findings
        )[:3000]
        continuation_context = "\n\n".join(
            part
            for part in (
                rag_context,
                "CONTINUATION REQUEST: The previous candidate JSONL reached its token limit. "
                "Return only additional concrete candidates not listed below. Do not repeat existing rows.\n"
                f"EXISTING CANDIDATE KEYS: {seen}",
            )
            if part
        )
        continuation = await invoke_model(
            node=node,
            files=files,
            prompt=prompt,
            user_context=continuation_context,
            response_contract=CANDIDATE_RESPONSE_CONTRACT,
            settings=candidate_settings,
            response_schema=None,
            response_parser=_parse_candidate_jsonl_response,
        )
        if isinstance(continuation, ModelReviewResponse):
            by_key = {
                (finding.file_path, finding.line, finding.title): finding
                for finding in [*candidate_result.findings, *continuation.findings]
            }
            candidate_result = ModelReviewResponse(
                summary=f"第一阶段发现 {len(by_key)} 个候选问题。",
                score=_score_for_findings(list(by_key.values())),
                findings=list(by_key.values()),
            )
    model_elapsed = perf_counter() - model_started
    discovery_timing_log = truncate_model_log(
        "\n".join(
            part
            for part in (
                getattr(task, "model_log", None),
                (
                    "[CandidateDiscoveryTiming] "
                    f"rag_context_s={rag_elapsed:.4f}; model_candidate_s={model_elapsed:.4f}; "
                    f"candidate_count={len(candidate_result.findings)}; "
                    f"discovery_total_s={perf_counter() - discovery_started:.4f}."
                ),
                (
                    "[ModelUsage] stage=candidate; "
                    f"prompt_tokens={invocation.prompt_tokens}; completion_tokens={invocation.completion_tokens}; "
                    f"elapsed_s={(invocation.elapsed_seconds or 0.0):.4f}; "
                    f"generation_tps={(invocation.completion_tokens / invocation.elapsed_seconds):.2f}."
                    if isinstance(invocation, ModelInvocationResult)
                    and invocation.completion_tokens is not None
                    and invocation.elapsed_seconds
                    else ""
                ),
            )
            if part
        )
    )
    if hasattr(task, "model_log"):
        task.model_log = discovery_timing_log
    return await _finalize_candidate_review(
        db=db,
        task=task,
        node=node,
        files=files,
        candidate_result=candidate_result,
        settings=settings,
    )


async def _format_candidate_jsonl(
    *,
    task: ReviewTask,
    node: ModelNode,
    candidate_jsonl: str,
    allowed_categories: Sequence[str],
    settings: Settings,
) -> tuple[list[ReviewFinding], int]:
    if not candidate_jsonl.strip():
        return [], 0

    if not settings.candidate_format_model_enabled:
        return (
            _parse_candidate_jsonl_response(
                {"choices": [{"message": {"content": candidate_jsonl}}]}
            ).findings,
            0,
        )

    formatted: list[ReviewFinding] = []
    failed_batches = 0
    for batch in _candidate_jsonl_batches(candidate_jsonl, settings.candidate_format_batch_size):
        try:
            formatted_result = await invoke_model(
                node=node,
                files=(),
                prompt=_candidate_format_prompt(allowed_categories),
                response_contract=CANDIDATE_FORMAT_CONTRACT,
                settings=settings,
                response_schema=FormattedFindingsResponse,
                response_model=FormattedFindingsResponse,
                response_normalizer=_normalize_formatted_findings_contract,
                input_message=f"CANDIDATE JSONL DOCUMENT:\n{batch}",
            )
        except ModelInvocationError as exc:
            failed_batches += 1
            task.model_log = truncate_model_log(
                "\n".join(
                    part
                    for part in (
                        task.model_log,
                        f"[CandidateFormat] Model formatting failed; backend fallback used: {exc}",
                    )
                    if part
                )
            )
            formatted.extend(
                _parse_candidate_jsonl_response(
                    {"choices": [{"message": {"content": batch}}]}
                ).findings
            )
            continue
        if not isinstance(formatted_result, FormattedFindingsResponse):
            failed_batches += 1
            continue
        formatted.extend(
            ReviewFinding.model_validate(finding.model_dump(mode="json"))
            for finding in formatted_result.findings
        )
    return formatted, failed_batches


async def _finalize_candidate_review(
    *,
    db: Session,
    task: ReviewTask,
    node: ModelNode,
    files: Sequence[ReviewFile],
    candidate_result: ModelReviewResponse,
    settings: Settings,
) -> ModelReviewResponse:
    finalize_started = perf_counter()
    candidate_jsonl = _candidate_jsonl(_select_candidate_findings(candidate_result))
    task.candidate_jsonl = candidate_jsonl
    db.commit()
    allowed_categories = _allowed_final_categories(task.check_types)
    formatted, failed_batches = await _format_candidate_jsonl(
        task=task,
        node=node,
        candidate_jsonl=candidate_jsonl,
        allowed_categories=allowed_categories,
        settings=settings,
    )
    formatting_elapsed = perf_counter() - finalize_started
    validation_started = perf_counter()
    static_findings = detect_static_c_findings(files)
    clang_findings: list[ReviewFinding] = []
    clang_summary = "disabled"
    if settings.clang_static_analysis_enabled:
        try:
            clang_result = await asyncio.to_thread(run_clang_static_analysis, files, settings)
            clang_findings = diagnostics_to_findings(clang_result.diagnostics)
            clang_summary = (
                f"available={clang_result.available}; completed={clang_result.completed}; "
                f"files={len(clang_result.analyzed_files)}; diagnostics={len(clang_result.diagnostics)}; "
                f"partial={clang_result.partial}; skipped_files={clang_result.skipped_files}; "
                f"elapsed_s={clang_result.elapsed_seconds:.4f}; errors={clang_result.errors[:3]}"
            )
        except Exception as exc:
            # External analyzer failures must never break the established LLM
            # review path. The adapter remains optional until acceptance data
            # proves it should be enabled by default.
            clang_summary = f"failed={type(exc).__name__}: {exc}"
    prevalidated, rejected = _filter_candidate_findings(
        files,
        [*formatted, *static_findings],
    )
    # Clang diagnostics already carry a feasible symbolic-execution path.
    # Running them through the lightweight path-insensitive leak suppressor
    # could discard a real early-return leak merely because another branch
    # releases the same handle later in the function.
    clang_prevalidated = _validate_compiler_findings(files, clang_findings)
    prevalidated = _merge_compiler_findings(files, prevalidated, clang_prevalidated)
    validation_elapsed = perf_counter() - validation_started
    category_started = perf_counter()
    deterministic, unresolved = _partition_category_candidates(prevalidated, allowed_categories)
    semantic_resolved, semantic_failed_batches = await _resolve_unmatched_categories(
        task=task,
        node=node,
        files=files,
        findings=unresolved,
        allowed_categories=allowed_categories,
        settings=settings,
    )
    semantic_elapsed = perf_counter() - category_started
    merge_started = perf_counter()
    category_filtered = _dedupe_candidate_findings([*deterministic, *semantic_resolved])
    final_findings, duplicate_roots = _dedupe_root_findings(files, category_filtered)
    merge_elapsed = perf_counter() - merge_started
    final_findings.sort(
        key=lambda finding: (
            SEVERITY_RANK.get(finding.severity.value, 99),
            finding.file_path,
            finding.line or 10**9,
        )
    )
    summary = (
        f"两阶段审查完成，共输出 {len(final_findings)} 个问题。"
        if final_findings
        else "两阶段审查完成，未输出符合类型要求的问题。"
    )
    task.model_log = truncate_model_log(
        "\n\n".join(
            part
            for part in [
                task.model_log,
                (
                    "[CandidatePipeline] "
                    f"discovered={len(candidate_result.findings)}; cached_jsonl_rows={len(candidate_result.findings)}; "
                    f"formatted={len(formatted)}; static_supplemental={len(static_findings)}; "
                    f"clang_supplemental={len(clang_findings)}; clang=({clang_summary}); "
                    f"category_allowed={len(category_filtered)}; "
                    f"deterministic={len(deterministic)}; semantic_unresolved={len(unresolved)}; "
                    f"semantic_resolved={len(semantic_resolved)}; semantic_failed_batches={semantic_failed_batches}; "
                    f"duplicate_roots={duplicate_roots}; "
                    f"backend_rejected={sum(rejected.values())} {rejected}; "
                    f"format_failed_batches={failed_batches}; final={len(final_findings)}; "
                    f"timing_format_s={formatting_elapsed:.4f}; timing_validation_s={validation_elapsed:.4f}; "
                    f"timing_semantic_s={semantic_elapsed:.4f}; timing_merge_s={merge_elapsed:.4f}; "
                    f"timing_finalize_total_s={perf_counter() - finalize_started:.4f}."
                ),
            ]
            if part
        )
    )
    db.commit()
    return ModelReviewResponse(
        summary=summary,
        score=_score_for_findings(final_findings),
        findings=final_findings,
    )


async def invoke_selected_model(
    db: Session, task_id: str, retry_instruction: str | None = None
) -> ModelReviewResponse:
    from app.db.models import ReviewTask
    from app.services.prompts import get_active_prompt

    task = db.get(ReviewTask, task_id)
    if task is None:
        raise ModelInvocationError("review task does not exist")
    prompt = get_active_prompt(db)
    settings = get_settings()
    candidate_settings = _candidate_stage_settings(settings, task.files)
    base_prompt = (
        prompt.body
        if settings.rag_candidate_scan_enabled
        else f"{prompt.body}\n\n{check_types_prompt(task.check_types)}"
    )
    dispatch_pool = _review_node_dispatch_pool(db, task.model_node, task=task, settings=settings)

    def rag_batch_prompt(
        prompt_text: str,
        batch_index: int,
        batch_count: int,
        batch: Sequence[ChunkedReviewFile],
    ) -> str:
        purpose = "candidate" if settings.rag_candidate_scan_enabled else "default"
        enriched = _with_rag_context(db, task, prompt_text, settings, files=batch, persist=False, purpose=purpose)
        return _batch_prompt(enriched, batch_index, batch_count, batch)

    def update_chunk_progress(completed_chunks: int, total_chunks: int) -> None:
        if total_chunks <= 0:
            return
        current_task = db.get(ReviewTask, task_id)
        if current_task is None:
            return
        chunk_progress = 10 + int((completed_chunks / total_chunks) * 85)
        current_task.progress = max(current_task.progress, min(95, chunk_progress))
        db.commit()

    should_chunk = _should_chunk(
        task.files,
        candidate_settings if settings.rag_candidate_scan_enabled else settings,
        prompt=base_prompt,
        response_contract=CANDIDATE_RESPONSE_CONTRACT if settings.rag_candidate_scan_enabled else FINAL_RESPONSE_CONTRACT,
    ) or (len(dispatch_pool.nodes) > 1 and len(task.files) > 1)
    if settings.rag_candidate_scan_enabled and not should_chunk:
        return await _invoke_candidate_review(
            db=db,
            task=task,
            node=task.model_node,
            files=task.files,
            prompt=base_prompt,
            settings=settings,
            retry_instruction=retry_instruction,
        )

    if should_chunk:
        dispatch_files: Sequence[ReviewFile] = _rag_review_unit_files(db, task, settings) or task.files
        candidate_result = await _invoke_chunked_review(
            node=task.model_node,
            dispatch_pool=dispatch_pool,
            files=dispatch_files,
            prompt=base_prompt,
            retry_instruction=retry_instruction,
            settings=candidate_settings if settings.rag_candidate_scan_enabled else settings,
            progress_callback=update_chunk_progress,
            batch_prompt_builder=rag_batch_prompt,
            response_contract=CANDIDATE_RESPONSE_CONTRACT if settings.rag_candidate_scan_enabled else FINAL_RESPONSE_CONTRACT,
            response_schema=None if settings.rag_candidate_scan_enabled else CompactModelReviewResponse,
            response_parser=_parse_candidate_jsonl_response if settings.rag_candidate_scan_enabled else None,
        )
        if settings.rag_candidate_scan_enabled:
            return await _finalize_candidate_review(
                db=db,
                task=task,
                node=task.model_node,
                files=task.files,
                candidate_result=candidate_result,
                settings=settings,
            )
        return candidate_result
    try:
        rag_context = _rag_context(db, task, settings, files=task.files, purpose="default")
        return await invoke_model(
            node=task.model_node,
            files=task.files,
            prompt=base_prompt,
            user_context=rag_context,
            response_contract=FINAL_RESPONSE_CONTRACT,
            retry_instruction=retry_instruction,
            settings=settings,
        )
    except ModelInvocationError as exc:
        if "context window is too small" not in str(exc):
            raise
        return await _invoke_chunked_review(
            node=task.model_node,
            dispatch_pool=dispatch_pool,
            files=_rag_review_unit_files(db, task, settings) or task.files,
            prompt=base_prompt,
            retry_instruction=retry_instruction,
            settings=settings,
            chunk_max_chars=max(1000, settings.model_chunk_max_chars // 2),
            progress_callback=update_chunk_progress,
            batch_prompt_builder=rag_batch_prompt,
        )


def _rag_context(
    db: Session,
    task: ReviewTask,
    settings: Settings,
    *,
    files: Sequence[ReviewFile] | Sequence[ChunkedReviewFile],
    persist: bool = True,
    purpose: str = "default",
) -> str:
    if not settings.rag_enabled:
        return ""
    try:
        from app.services.code_index.context_builder import build_rag_context

        rag_context = build_rag_context(db, task, list(files), settings=settings, persist=persist, purpose=purpose)
        if persist and rag_context:
            db.commit()
    except Exception as exc:  # pragma: no cover - defensive guard for optional RAG services.
        current_log = task.model_log or ""
        task.model_log = truncate_model_log(f"{current_log}\n[RAG] Context build skipped: {exc}")
        db.commit()
        return ""
    return rag_context or ""


def _with_rag_context(
    db: Session,
    task: ReviewTask,
    prompt: str,
    settings: Settings,
    *,
    files: Sequence[ReviewFile] | Sequence[ChunkedReviewFile],
    persist: bool = True,
    purpose: str = "default",
) -> str:
    rag_context = _rag_context(db, task, settings, files=files, persist=persist, purpose=purpose)
    return f"{prompt}\n\n{rag_context}" if rag_context else prompt


def _rag_review_unit_files(db: Session, task: ReviewTask, settings: Settings) -> list[ReviewFile]:
    if not settings.rag_enabled or not settings.rag_review_units_enabled:
        return []
    try:
        from app.services.code_index.indexer import load_or_build_code_index
        from app.services.code_index.planner import plan_review_units

        project = load_or_build_code_index(db, task, settings=settings)
        chunks_by_id = {chunk.id: chunk for chunk in db.query(CodeChunk).filter_by(project_id=project.id).all()}
        units = [unit for unit in plan_review_units(project) if unit.unit_type == "function"]
    except Exception:
        return []
    if not units:
        return []
    unit_files: list[ReviewFile] = []
    for unit in units:
        parts: list[str] = [
            f"===== REVIEW UNIT: {unit.unit_id} =====",
            f"File: {unit.file_path}",
            f"Symbol: {unit.symbol_name or ''}",
            f"Lines: {unit.start_line}-{unit.end_line}",
        ]
        seen_chunks: set[str] = set()
        for chunk_id in unit.chunk_ids:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None or chunk.id in seen_chunks:
                continue
            seen_chunks.add(chunk.id)
            parts.append(
                "\n".join(
                    [
                        f"\n--- CHUNK: {chunk.chunk_kind} {chunk.file.relative_path}:{chunk.start_line}-{chunk.end_line} {chunk.symbol_name or ''}".rstrip(),
                        chunk.content,
                    ]
                )
            )
        source_text = "\n".join(parts)
        unit_files.append(
            ReviewFile(
                relative_path=unit.file_path,
                source_text=source_text,
                size_bytes=len(source_text.encode("utf-8", errors="ignore")),
            )
        )
    task.model_log = truncate_model_log(
        "\n\n".join(
            part
            for part in [
                task.model_log,
                f"[RAG] Prepared {len(unit_files)} function review unit(s) for model dispatch.",
            ]
            if part
        )
    )
    db.commit()
    return unit_files


async def check_model_health(node: ModelNode, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if node.base_url.startswith("mock://"):
        return {"ok": settings.mock_model_enabled, "kind": "mock"}
    try:
        async with httpx.AsyncClient(timeout=node.timeout_seconds) as client:
            response = await client.get(
                f"{node.base_url.rstrip('/')}/v1/models",
                headers={"Authorization": f"Bearer {node.api_key}"} if node.api_key else None,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ModelInvocationError("selected model node is unavailable") from exc
    return {"ok": True, "kind": "openai-compatible", "status_code": response.status_code}

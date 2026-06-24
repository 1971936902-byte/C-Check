from __future__ import annotations

import json
import math
import re
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, ModelNode, ReviewFile, ReviewTask, TaskStatus
from app.schemas.model_response import ModelReviewResponse
from app.services.check_types import check_types_prompt


MAX_MODEL_LOG_CHARS = 12000
RESPONSE_REQUIRED_KEYS = {"summary", "score", "findings"}
STRUCTURED_RESPONSE_SCHEMA_NAME = "c_review_response"
TOKEN_BUDGET_SAFETY_MARGIN = 128
INPUT_TOKEN_SAFETY_MARGIN = 512
MIN_RETRY_OUTPUT_TOKENS = 128
CHUNK_CONTEXT_CHAR_RATIO = 0.70
MIN_CHUNK_CONTEXT_CHARS = 1000
CHUNK_PROMPT_CHAR_MARGIN = 512
CHUNK_LINE_PREFIX_WIDTH = 6
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "suggestion": 3}
TOKEN_BUDGET_PATTERN = re.compile(
    r"maximum context length is (?P<context>\d+) tokens and your request has (?P<input>\d+) input tokens",
    re.IGNORECASE,
)
VLLM_TOKEN_BUDGET_PATTERN = re.compile(
    r"maximum context length is (?P<context>\d+) tokens\..*?requested (?P<requested>\d+) tokens "
    r"\((?P<input>\d+) in the messages, (?P<completion>\d+) in the completion\)",
    re.IGNORECASE,
)
RESPONSE_CONTRACT = """
Return exactly one compact JSON object. No Markdown.
Top-level keys: summary, score, findings.
Use Chinese. Keep summary under 80 Chinese chars.
Return up to 12 high-value findings for this request, only concrete C defects.
Each finding uses: severity, category, title, description, file_path, line, evidence_ids, call_chain, confidence, remediation, code_snippet, fixed_snippet.
Keep title under 40 chars. Keep description and remediation under 120 Chinese chars each.
The line value must point to the exact visible statement or declaration causing the issue.
Do not use pure data initializer rows, lookup tables, font/bitmap tables, or comments as finding locations.
Use code_snippet/fixed_snippet as [] unless one line is essential; then include at most one line.
Use evidence_ids as [] unless Evidence E1/E2 etc directly supports the finding.
Use call_chain as [] unless the finding depends on an inter-function call path.
Use lowercase enum values exactly. Use null for line only when no precise line exists.
Before producing findings, internally scan these categories in order:
1. integer overflow, integer underflow, truncation, divide-by-zero, and unsafe size calculations;
2. memcpy/memmove/strcpy-style copy bounds, fixed-array or heap out-of-bounds read/write, and malloc(n) followed by access at [n];
3. malloc/free lifetime issues: double free, use-after-free, dangling pointer, and free followed by read/write;
4. resource leaks: pointer overwritten with 0/NULL before free, missing free/close/unlock on visible paths;
5. resource exhaustion: infinite recursion, large recursive stack frames, unbounded allocation loops, and input-controlled huge allocation.
If different defect categories appear in the same function, report them separately.
Do not stop after the first obvious memcpy, malloc, or free issue.
Do not merge double free with use-after-free, or out-of-bounds read with out-of-bounds write.
Dedupe exact duplicates, but preserve different consequences from the same root cause.
"""


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
class ModelNodeDispatchPool:
    nodes: tuple[ModelNode, ...]
    base_loads: dict[str, int]


def _mock_response(files: Sequence[ReviewFile]) -> ModelReviewResponse:
    return ModelReviewResponse(
        summary=f"Mock review completed for {len(files)} source file(s).",
        score=100,
        findings=[],
    )


def _source_message(files: Sequence[ReviewFile]) -> str:
    sections = []
    for source in files:
        sections.append(f"===== FILE: {source.relative_path} =====\n{source.source_text}")
    return "\n\n".join(sections)


def _estimate_tokens(text: str, settings: Settings) -> int:
    return math.ceil(len(text) / settings.model_token_chars_per_token)


def _input_token_budget(settings: Settings) -> int:
    context_budget = settings.model_context_window - settings.model_max_tokens - INPUT_TOKEN_SAFETY_MARGIN
    return max(0, min(settings.model_max_input_tokens, context_budget))


def _response_format_overhead(settings: Settings) -> str:
    response_format = _response_format(settings)
    if response_format is None:
        return ""
    return json.dumps(response_format, ensure_ascii=False, separators=(",", ":"))


def _strict_prompt(
    prompt: str,
    retry_instruction: str | None = None,
) -> str:
    strict_prompt = f"{prompt}\n\n{RESPONSE_CONTRACT}"
    if retry_instruction:
        strict_prompt = (
            f"{strict_prompt}\n\nPrevious response was rejected by the backend validator:\n"
            f"{retry_instruction}\nReturn a corrected JSON object only."
        )
    return strict_prompt


def _input_budget_details(
    *,
    prompt: str,
    files: Sequence[ReviewFile],
    settings: Settings,
    retry_instruction: str | None = None,
) -> tuple[int, int]:
    input_text = "\n\n".join(
        part
        for part in (
            _strict_prompt(prompt, retry_instruction),
            _response_format_overhead(settings),
            _source_message(files),
        )
        if part
    )
    return _estimate_tokens(input_text, settings), _input_token_budget(settings)


def _source_char_budget(
    *,
    prompt: str,
    settings: Settings,
    retry_instruction: str | None = None,
) -> int:
    max_input_chars = int(_input_token_budget(settings) * settings.model_token_chars_per_token)
    overhead = len(_strict_prompt(prompt, retry_instruction)) + len(_response_format_overhead(settings))
    return max(1, max_input_chars - overhead - CHUNK_PROMPT_CHAR_MARGIN)


def _numbered_chunk_source(source_text: str, start_line: int, end_line: int) -> str:
    lines = source_text.splitlines()
    selected = lines[start_line - 1 : end_line]
    return "\n".join(
        f"{line_number:0{CHUNK_LINE_PREFIX_WIDTH}d}: {line}"
        for line_number, line in enumerate(selected, start=start_line)
    )


def _chunk_file(source: ReviewFile, max_chars: int) -> list[ChunkedReviewFile]:
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


def _chunk_review_files(files: Sequence[ReviewFile], settings: Settings) -> list[ChunkedReviewFile]:
    chunks: list[ChunkedReviewFile] = []
    max_chars = _effective_chunk_max_chars(settings)
    for source in files:
        chunks.extend(_chunk_file(source, max_chars))
    return chunks


def _chunk_payload_chars(chunk: ChunkedReviewFile) -> int:
    return len(f"===== FILE: {chunk.relative_path} =====\n{chunk.source_text}\n\n")


def _effective_chunk_max_chars(
    settings: Settings,
    *,
    prompt: str | None = None,
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
            retry_instruction=retry_instruction,
        ))
    return max(1, max_chars)


def _chunk_review_batches(
    files: Sequence[ReviewFile],
    settings: Settings,
    *,
    prompt: str | None = None,
    retry_instruction: str | None = None,
    isolate_chunks: bool = False,
) -> list[list[ChunkedReviewFile]]:
    batches: list[list[ChunkedReviewFile]] = []
    current_batch: list[ChunkedReviewFile] = []
    current_chars = 0
    max_chars = _effective_chunk_max_chars(
        settings,
        prompt=prompt,
        retry_instruction=retry_instruction,
    )

    chunks: list[ChunkedReviewFile] = []
    for source in files:
        chunks.extend(_chunk_file(source, max_chars))
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


def _should_chunk(files: Sequence[ReviewFile], settings: Settings) -> bool:
    return len(_source_message(files)) > _effective_chunk_max_chars(settings)


def _ensure_input_budget(
    *,
    prompt: str,
    files: Sequence[ReviewFile],
    settings: Settings,
    retry_instruction: str | None = None,
) -> None:
    estimated_tokens, budget_tokens = _input_budget_details(
        prompt=prompt,
        files=files,
        settings=settings,
        retry_instruction=retry_instruction,
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
) -> ModelReviewResponse:
    if chunk_max_chars is not None:
        settings = settings.model_copy(update={"model_chunk_max_chars": chunk_max_chars})
    while True:
        batches = _chunk_review_batches(
            files,
            settings,
            prompt=prompt,
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
                        )
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


def _is_contract_object(value: Any) -> bool:
    return isinstance(value, dict) and RESPONSE_REQUIRED_KEYS.issubset(value)


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


def _extract_json_object(content: str) -> str:
    stripped = _strip_code_fence(content)
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if _is_contract_object(parsed):
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
            if index == 0 and any(f'"{key}"' in stripped for key in RESPONSE_REQUIRED_KEYS):
                found_partial_contract = True
            continue
        if isinstance(parsed, dict):
            found_json_object = True
            if _is_contract_object(parsed):
                return stripped[index : index + end]
    if found_partial_contract:
        recovered = _recover_truncated_contract(stripped)
        if recovered is not None:
            return recovered
        raise ValueError(
            "model response contains a truncated top-level JSON object; no complete top-level JSON object with summary, score, and findings was found"
        )
    if found_json_object:
        raise ValueError(
            "model response contains JSON fragments, but no complete top-level JSON object with summary, score, and findings was found"
        )
    return stripped


def _normalize_model_contract(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    findings = value.get("findings")
    if not isinstance(findings, list):
        return value
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = finding.get("severity")
        if isinstance(severity, str):
            normalized_severity = {
                "高": "high",
                "高危": "high",
                "严重": "high",
                "中": "medium",
                "中危": "medium",
                "中等": "medium",
                "低": "low",
                "低危": "low",
                "建议": "suggestion",
                "提示": "suggestion",
            }.get(severity.strip())
            if normalized_severity is not None:
                finding["severity"] = normalized_severity
        category = finding.get("category")
        if isinstance(category, str):
            normalized_category = {
                "内存安全": "memory_safety",
                "内存安全问题": "memory_safety",
                "缓冲区溢出": "buffer_overflow",
                "堆缓冲区溢出": "buffer_overflow",
                "栈缓冲区溢出": "buffer_overflow",
                "指针安全": "pointer_safety",
                "空指针": "pointer_safety",
                "野指针": "pointer_safety",
                "资源泄漏": "resource_leak",
                "内存泄漏": "resource_leak",
                "逻辑错误": "logic",
                "逻辑": "logic",
                "安全": "security",
                "输入校验": "input_validation",
                "输入验证": "input_validation",
                "整数安全": "integer_safety",
                "整数溢出": "integer_safety",
                "整数溢出与类型转换": "integer_safety",
                "并发": "concurrency",
                "并发与线程安全": "concurrency",
                "性能": "performance",
                "代码风格": "style",
                "可维护性": "maintainability",
                "兼容性": "compatibility",
                "可移植性": "portability",
            }.get(category.strip())
            if normalized_category is not None:
                finding["category"] = normalized_category
        fallback_line = finding.get("line")
        if not isinstance(fallback_line, int):
            fallback_line = None
        for snippet_key in ("code_snippet", "fixed_snippet"):
            snippet = finding.get(snippet_key)
            if not isinstance(snippet, list):
                continue
            normalized_lines = []
            for line in snippet:
                if isinstance(line, str):
                    if fallback_line is None:
                        continue
                    line = {"line": fallback_line, "content": line, "kind": "context"}
                elif not isinstance(line, dict):
                    continue
                if line.get("kind") not in {"context", "removed", "added"}:
                    line = {**line, "kind": "context"}
                if line.get("line") is None:
                    if fallback_line is None:
                        continue
                    line = {**line, "line": fallback_line}
                normalized_lines.append(line)
            finding[snippet_key] = normalized_lines
        if not isinstance(finding.get("evidence_ids"), list):
            finding["evidence_ids"] = []
        else:
            finding["evidence_ids"] = [
                item for item in finding["evidence_ids"] if isinstance(item, str) and item.startswith("E")
            ][:12]
        if not isinstance(finding.get("call_chain"), list):
            finding["call_chain"] = []
        else:
            finding["call_chain"] = [item for item in finding["call_chain"] if isinstance(item, str) and item][:16]
        confidence = finding.get("confidence")
        if confidence is not None and not isinstance(confidence, (int, float)):
            finding["confidence"] = None
        elif isinstance(confidence, (int, float)) and confidence > 1:
            finding["confidence"] = max(0, min(1, float(confidence) / 100))
    return value


def _parse_response(payload: dict[str, Any]) -> ModelReviewResponse:
    content: str | None = None
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("assistant content is not text")
        return ModelReviewResponse.model_validate(
            _normalize_model_contract(json.loads(_extract_json_object(content)))
        )
    except (KeyError, IndexError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise ModelInvocationError(
            "model returned an invalid structured response",
            raw_response=content or json.dumps(payload, ensure_ascii=False),
            details=str(exc),
        ) from exc


def _response_format(settings: Settings) -> dict[str, Any] | None:
    if not settings.model_structured_outputs_enabled:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRUCTURED_RESPONSE_SCHEMA_NAME,
            "strict": True,
            "schema": ModelReviewResponse.model_json_schema(),
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
    retry_instruction: str | None = None,
    settings: Settings | None = None,
) -> ModelReviewResponse:
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
        retry_instruction=retry_instruction,
    )
    strict_prompt = _strict_prompt(prompt, retry_instruction)
    body = {
        "model": node.model_identifier,
        "messages": [
            {"role": "system", "content": strict_prompt},
            {"role": "user", "content": _source_message(files)},
        ],
        "temperature": 0,
        "max_tokens": settings.model_max_tokens,
    }
    response_format = _response_format(settings)
    if response_format is not None:
        body["response_format"] = response_format
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
    return _parse_response(payload)


async def invoke_selected_model(
    db: Session, task_id: str, retry_instruction: str | None = None
) -> ModelReviewResponse:
    from app.db.models import ReviewTask
    from app.services.prompts import get_active_prompt

    task = db.get(ReviewTask, task_id)
    if task is None:
        raise ModelInvocationError("review task does not exist")
    prompt = get_active_prompt(db)
    base_prompt = f"{prompt.body}\n\n{check_types_prompt(task.check_types)}"
    settings = get_settings()
    dispatch_pool = _review_node_dispatch_pool(db, task.model_node, task=task, settings=settings)

    def rag_batch_prompt(
        prompt_text: str,
        batch_index: int,
        batch_count: int,
        batch: Sequence[ChunkedReviewFile],
    ) -> str:
        enriched = _with_rag_context(db, task, prompt_text, settings, files=batch, persist=False)
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

    if _should_chunk(task.files, settings) or (len(dispatch_pool.nodes) > 1 and len(task.files) > 1):
        dispatch_files: Sequence[ReviewFile] = _rag_review_unit_files(db, task, settings) or task.files
        return await _invoke_chunked_review(
            node=task.model_node,
            dispatch_pool=dispatch_pool,
            files=dispatch_files,
            prompt=base_prompt,
            retry_instruction=retry_instruction,
            settings=settings,
            progress_callback=update_chunk_progress,
            batch_prompt_builder=rag_batch_prompt,
        )
    try:
        return await invoke_model(
            node=task.model_node,
            files=task.files,
            prompt=_with_rag_context(db, task, base_prompt, settings, files=task.files),
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


def _with_rag_context(
    db: Session,
    task: ReviewTask,
    prompt: str,
    settings: Settings,
    *,
    files: Sequence[ReviewFile] | Sequence[ChunkedReviewFile],
    persist: bool = True,
) -> str:
    if not settings.rag_enabled:
        return prompt
    try:
        from app.services.code_index.context_builder import build_rag_context

        rag_context = build_rag_context(db, task, list(files), settings=settings, persist=persist)
    except Exception as exc:  # pragma: no cover - defensive guard for optional RAG services.
        current_log = task.model_log or ""
        task.model_log = truncate_model_log(f"{current_log}\n[RAG] Context build skipped: {exc}")
        db.commit()
        return prompt
    if not rag_context:
        return prompt
    return f"{prompt}\n\n{rag_context}"


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

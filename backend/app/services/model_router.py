from __future__ import annotations

import json
import re
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ModelNode, ReviewFile
from app.schemas.model_response import ModelReviewResponse
from app.services.check_types import check_types_prompt


MAX_MODEL_LOG_CHARS = 12000
RESPONSE_REQUIRED_KEYS = {"summary", "score", "findings"}
STRUCTURED_RESPONSE_SCHEMA_NAME = "c_review_response"
TOKEN_BUDGET_SAFETY_MARGIN = 128
MIN_RETRY_OUTPUT_TOKENS = 128
CHUNK_CONTEXT_CHAR_RATIO = 0.45
MIN_CHUNK_CONTEXT_CHARS = 1000
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
Return at most 3 findings for this request, only concrete C defects.
Each finding uses: severity, category, title, description, file_path, line, remediation, code_snippet, fixed_snippet.
Keep title under 40 chars. Keep description and remediation under 120 Chinese chars each.
Use code_snippet/fixed_snippet as [] unless one line is essential; then include at most one line.
Use lowercase enum values exactly. Use null for line only when no precise line exists.
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


def _effective_chunk_max_chars(settings: Settings) -> int:
    conservative_budget = int(settings.model_chunk_max_chars * CHUNK_CONTEXT_CHAR_RATIO)
    if settings.model_chunk_max_chars >= MIN_CHUNK_CONTEXT_CHARS:
        conservative_budget = max(MIN_CHUNK_CONTEXT_CHARS, conservative_budget)
    return max(1, min(settings.model_chunk_max_chars, conservative_budget))


def _chunk_review_batches(files: Sequence[ReviewFile], settings: Settings) -> list[list[ChunkedReviewFile]]:
    batches: list[list[ChunkedReviewFile]] = []
    current_batch: list[ChunkedReviewFile] = []
    current_chars = 0
    max_chars = _effective_chunk_max_chars(settings)

    for chunk in _chunk_review_files(files, settings):
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


def _should_chunk(files: Sequence[ReviewFile], settings: Settings) -> bool:
    return len(_source_message(files)) > _effective_chunk_max_chars(settings)


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
        "The submitted code is being reviewed in chunks because it is too large for one model "
        "context window. Review only this batch and report concrete issues visible in this "
        "batch. Each source line is prefixed as `000123: code`; use the numeric prefix as the "
        "`line` value and keep `file_path` as the original file path.\n"
        f"Batch {batch_index} of {batch_count}, containing {len(batch)} source chunk(s):\n"
        f"{chunk_lines}"
    )


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
    files: Sequence[ReviewFile],
    prompt: str,
    settings: Settings,
    retry_instruction: str | None = None,
    chunk_max_chars: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ModelReviewResponse:
    if chunk_max_chars is not None:
        settings = settings.model_copy(update={"model_chunk_max_chars": chunk_max_chars})
    while True:
        batches = _chunk_review_batches(files, settings)
        indexed_results: list[tuple[int, ModelReviewResponse]] = []
        semaphore = asyncio.Semaphore(settings.model_chunk_concurrency)

        async def invoke_batch(index: int, batch: Sequence[ChunkedReviewFile]) -> tuple[int, ModelReviewResponse]:
            async with semaphore:
                result = await invoke_model(
                    node=node,
                    files=list(batch),  # type: ignore[list-item]
                    prompt=_batch_prompt(prompt, index, len(batches), batch),
                    retry_instruction=retry_instruction,
                    settings=settings,
                )
                return index, result

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
        fallback_line = finding.get("line")
        if not isinstance(fallback_line, int):
            fallback_line = None
        for snippet_key in ("code_snippet", "fixed_snippet"):
            snippet = finding.get(snippet_key)
            if not isinstance(snippet, list):
                continue
            normalized_lines = []
            for line in snippet:
                if not isinstance(line, dict):
                    continue
                if line.get("kind") not in {"context", "removed", "added"}:
                    line = {**line, "kind": "context"}
                if line.get("line") is None:
                    if fallback_line is None:
                        continue
                    line = {**line, "line": fallback_line}
                normalized_lines.append(line)
            finding[snippet_key] = normalized_lines
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
    strict_prompt = f"{prompt}\n\n{RESPONSE_CONTRACT}"
    if retry_instruction:
        strict_prompt = f"{strict_prompt}\n\nPrevious response was rejected by the backend validator:\n{retry_instruction}\nReturn a corrected JSON object only."
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
    scoped_prompt = f"{prompt.body}\n\n{check_types_prompt(task.check_types)}"
    settings = get_settings()

    def update_chunk_progress(completed_chunks: int, total_chunks: int) -> None:
        if total_chunks <= 0:
            return
        current_task = db.get(ReviewTask, task_id)
        if current_task is None:
            return
        chunk_progress = 10 + int((completed_chunks / total_chunks) * 85)
        current_task.progress = max(current_task.progress, min(95, chunk_progress))
        db.commit()

    if _should_chunk(task.files, settings):
        return await _invoke_chunked_review(
            node=task.model_node,
            files=task.files,
            prompt=scoped_prompt,
            retry_instruction=retry_instruction,
            settings=settings,
            progress_callback=update_chunk_progress,
        )
    try:
        return await invoke_model(
            node=task.model_node,
            files=task.files,
            prompt=scoped_prompt,
            retry_instruction=retry_instruction,
            settings=settings,
        )
    except ModelInvocationError as exc:
        if "context window is too small" not in str(exc):
            raise
        return await _invoke_chunked_review(
            node=task.model_node,
            files=task.files,
            prompt=scoped_prompt,
            retry_instruction=retry_instruction,
            settings=settings,
            chunk_max_chars=max(1000, settings.model_chunk_max_chars // 2),
            progress_callback=update_chunk_progress,
        )


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

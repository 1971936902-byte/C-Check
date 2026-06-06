from __future__ import annotations

import json
import re
from collections.abc import Sequence
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
TOKEN_BUDGET_PATTERN = re.compile(
    r"maximum context length is (?P<context>\d+) tokens and your request has (?P<input>\d+) input tokens",
    re.IGNORECASE,
)
RESPONSE_CONTRACT = """
You must return exactly one JSON object and nothing else. Do not wrap it in Markdown.
The JSON object must match this schema:
{
  "summary": "string, concise Chinese review summary",
  "score": 0-100,
  "findings": [
    {
      "severity": "high | medium | low | suggestion",
      "category": "memory_safety | buffer_overflow | pointer_safety | resource_leak | logic | security | input_validation | integer_safety | concurrency | performance | style | maintainability | compatibility | portability",
      "title": "string",
      "description": "string",
      "file_path": "relative file path from the input",
      "line": 1,
      "remediation": "string",
      "code_snippet": [
        { "line": 1, "content": "original code line", "kind": "context | removed" }
      ],
      "fixed_snippet": [
        { "line": 1, "content": "fixed code line", "kind": "context | added" }
      ]
    }
  ]
}
Use null for "line" only when the finding cannot be tied to a specific line.
Use an empty findings array when no issue is found.
All enum values must be lowercase exactly as listed.
All strings must be valid JSON strings with escaped quotes and newlines.
Return at most 8 findings. Prioritize high-risk and concrete C language defects.
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


def truncate_model_log(value: str | None, limit: int = MAX_MODEL_LOG_CHARS) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... [truncated {len(value) - limit} chars]"


def _is_contract_object(value: Any) -> bool:
    return isinstance(value, dict) and RESPONSE_REQUIRED_KEYS.issubset(value)


def _extract_json_object(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
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
        raise ValueError(
            "model response contains a truncated top-level JSON object; no complete top-level JSON object with summary, score, and findings was found"
        )
    if found_json_object:
        raise ValueError(
            "model response contains JSON fragments, but no complete top-level JSON object with summary, score, and findings was found"
        )
    return stripped


def _parse_response(payload: dict[str, Any]) -> ModelReviewResponse:
    content: str | None = None
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("assistant content is not text")
        return ModelReviewResponse.model_validate_json(_extract_json_object(content))
    except (KeyError, IndexError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise ModelInvocationError(
            "model returned an invalid structured response",
            raw_response=content or json.dumps(payload, ensure_ascii=False),
            details=str(exc),
        ) from exc


def _response_format(settings: Settings) -> dict[str, Any]:
    if not settings.model_structured_outputs_enabled:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRUCTURED_RESPONSE_SCHEMA_NAME,
            "strict": True,
            "schema": ModelReviewResponse.model_json_schema(),
        },
    }


def _reduced_output_budget_from_error(error_text: str, current_max_tokens: int) -> int | None:
    match = TOKEN_BUDGET_PATTERN.search(error_text)
    if match is None:
        return None
    context_window = int(match.group("context"))
    input_tokens = int(match.group("input"))
    available = context_window - input_tokens - TOKEN_BUDGET_SAFETY_MARGIN
    if available < MIN_RETRY_OUTPUT_TOKENS:
        return None
    reduced = min(current_max_tokens - 1, available)
    return reduced if reduced >= MIN_RETRY_OUTPUT_TOKENS else None


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
        "response_format": _response_format(settings),
    }
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
    return await invoke_model(
        node=task.model_node,
        files=task.files,
        prompt=scoped_prompt,
        retry_instruction=retry_instruction,
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

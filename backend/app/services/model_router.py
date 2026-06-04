from __future__ import annotations

import json
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
        return stripped

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            _, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return stripped[index : index + end]
    return stripped


def _parse_response(payload: dict[str, Any]) -> ModelReviewResponse:
    content: str | None = None
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("assistant content is not text")
        return ModelReviewResponse.model_validate_json(_extract_json_object(content))
    except (KeyError, IndexError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        raise ModelInvocationError(
            "model returned an invalid structured response",
            raw_response=content or json.dumps(payload, ensure_ascii=False),
            details=str(exc),
        ) from exc


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
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=node.timeout_seconds) as client:
            response = await client.post(
                f"{node.base_url.rstrip('/')}/v1/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
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

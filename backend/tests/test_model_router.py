import asyncio
import json

import httpx
import pytest

from app.db.models import ModelNode, ReviewFile
from app.schemas.model_response import FindingCategory
from app.services.model_router import ModelInvocationError, _parse_response, invoke_model


def test_parse_response_accepts_json_inside_markdown_fence():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": """```json
{
  "summary": "未发现明显问题。",
  "score": 100,
  "findings": []
}
```"""
                    }
                }
            ]
        }
    )

    assert parsed.summary == "未发现明显问题。"
    assert parsed.score == 100
    assert parsed.findings == []


def test_parse_response_error_keeps_raw_model_content():
    with pytest.raises(ModelInvocationError) as raised:
        _parse_response({"choices": [{"message": {"content": "not valid json"}}]})

    assert "invalid structured response" in str(raised.value)
    assert raised.value.raw_response == "not valid json"
    assert raised.value.details


def test_parse_response_rejects_nested_finding_from_truncated_response():
    content = """
{
  "summary": "代码存在问题。",
  "score": 20,
  "findings": [
    {
      "severity": "high",
      "category": "buffer_overflow",
      "title": "固定缓冲区写入缺少边界检查",
      "description": "strcpy 写入固定缓冲区。",
      "file_path": "CTest.c",
      "line": 14,
      "remediation": "使用带边界的拷贝函数。",
      "code_snippet": [
        { "line": 14, "content": "strcpy(buf, input);", "kind": "removed" }
      ],
      "fixed_snippet": [
        { "line": 14, "content": "snprintf(buf, sizeof(buf), \"%s\", input);", "kind": "added" }
      ]
    },
    {
      "severity": "high"
"""

    with pytest.raises(ModelInvocationError) as raised:
        _parse_response({"choices": [{"message": {"content": content}}]})

    assert "invalid structured response" in str(raised.value)
    assert "complete top-level JSON object" in (raised.value.details or "")
    assert raised.value.raw_response == content


def test_invoke_model_sends_output_token_budget(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"未发现明显问题。","score":100,"findings":[]}'
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", FakeClient)
    monkeypatch.setenv("MODEL_MAX_TOKENS", "3072")

    asyncio.run(
        invoke_model(
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[
                ReviewFile(
                    relative_path="main.c",
                    source_text="int main(void){return 0;}",
                    size_bytes=25,
                )
            ],
            prompt="review",
        )
    )

    assert captured["json"]["max_tokens"] == 3072


def test_invoke_model_requests_json_schema_structured_output(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","score":100,"findings":[]}'
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", FakeClient)

    asyncio.run(
        invoke_model(
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[
                ReviewFile(
                    relative_path="main.c",
                    source_text="int main(void){return 0;}",
                    size_bytes=25,
                )
            ],
            prompt="review",
        )
    )

    response_format = captured["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "c_review_response"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["properties"]["findings"]["maxItems"] == 8


def test_parse_response_rejects_too_many_findings_for_audit():
    finding = {
        "severity": "low",
        "category": "maintainability",
        "title": "style",
        "description": "description",
        "file_path": "main.c",
        "line": 1,
        "remediation": "remediation",
        "code_snippet": [],
        "fixed_snippet": [],
    }
    content = {
        "summary": "too many findings",
        "score": 60,
        "findings": [finding for _ in range(9)],
    }

    with pytest.raises(ModelInvocationError) as raised:
        _parse_response({"choices": [{"message": {"content": json.dumps(content)}}]})

    assert "invalid structured response" in str(raised.value)
    assert "at most 8 items" in (raised.value.details or "")


def test_invoke_model_keeps_http_error_response_body(monkeypatch):
    class FakeResponse:
        text = '{"error":"max_tokens is too large"}'

        def raise_for_status(self):
            request = httpx.Request("POST", "http://model.local/v1/chat/completions")
            response = httpx.Response(400, request=request, text=self.text)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            return FakeResponse()

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", FakeClient)

    with pytest.raises(ModelInvocationError) as raised:
        asyncio.run(
            invoke_model(
                node=ModelNode(
                    display_name="test",
                    model_identifier="qwen-test",
                    base_url="http://model.local",
                    is_enabled=True,
                ),
                files=[
                    ReviewFile(
                        relative_path="main.c",
                        source_text="int main(void){return 0;}",
                        size_bytes=25,
                    )
                ],
                prompt="review",
            )
        )

    assert "selected model node is unavailable" in str(raised.value)
    assert "max_tokens is too large" in (raised.value.details or "")


def test_invoke_model_retries_with_smaller_output_budget_when_context_is_tight(monkeypatch):
    requested_tokens: list[int] = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = (
                '{"error":{"message":"\'max_tokens\' or \'max_completion_tokens\' is too large: '
                "2048. This model's maximum context length is 4096 tokens and your request has "
                '2524 input tokens (2048 > 4096 - 2524). None"}}'
            )

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://model.local/v1/chat/completions")
                response = httpx.Response(self.status_code, request=request, text=self.text)
                raise httpx.HTTPStatusError("bad request", request=request, response=response)

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","score":100,"findings":[]}'
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            requested_tokens.append(json["max_tokens"])
            return FakeResponse(400 if len(requested_tokens) == 1 else 200)

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", FakeClient)

    result = asyncio.run(
        invoke_model(
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[
                ReviewFile(
                    relative_path="main.c",
                    source_text="int main(void){return 0;}",
                    size_bytes=25,
                )
            ],
            prompt="review",
        )
    )

    assert result.summary == "ok"
    assert requested_tokens == [2048, 1444]


def test_finding_category_accepts_frontend_check_type_values():
    assert FindingCategory.BUFFER_OVERFLOW.value == "buffer_overflow"
    assert FindingCategory.INTEGER_SAFETY.value == "integer_safety"
    assert FindingCategory.MAINTAINABILITY.value == "maintainability"

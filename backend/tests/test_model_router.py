import asyncio
import json

import httpx
import pytest

from app.db.models import ModelNode, ReviewFile
from app.core.config import Settings
from app.schemas.model_response import FindingCategory, ModelReviewResponse
from app.services.model_router import (
    ModelInvocationError,
    _chunk_file,
    _effective_chunk_max_chars,
    _invoke_chunked_review,
    _chunk_review_batches,
    _chunk_review_files,
    _merge_chunk_results,
    _parse_response,
    invoke_selected_model,
    invoke_model,
)


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


def test_parse_response_normalizes_null_snippet_line_to_finding_line():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "存在一个可维护性问题。",
                                "score": 90,
                                "findings": [
                                    {
                                        "severity": "low",
                                        "category": "logic",
                                        "title": "补充注释",
                                        "description": "模型给出的修复片段包含新增注释行。",
                                        "file_path": "src/misc.c",
                                        "line": 114,
                                        "remediation": "保留新增注释并使用发现行号兜底。",
                                        "code_snippet": [
                                            {
                                                "line": 114,
                                                "content": "uint32_t tmppriority = 0;",
                                                "kind": "context",
                                            }
                                        ],
                                        "fixed_snippet": [
                                            {
                                                "line": None,
                                                "content": "/* 初始化变量 */",
                                                "kind": "added",
                                            }
                                        ],
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )

    assert parsed.findings[0].fixed_snippet[0].line == 114


def test_parse_response_normalizes_unknown_snippet_kind_to_context():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "存在一个说明性修复建议。",
                                "score": 90,
                                "findings": [
                                    {
                                        "severity": "low",
                                        "category": "logic",
                                        "title": "补充说明",
                                        "description": "模型把注释行标记成 comment。",
                                        "file_path": "src/misc.c",
                                        "line": 114,
                                        "remediation": "将说明性行按上下文展示。",
                                        "code_snippet": [],
                                        "fixed_snippet": [
                                            {
                                                "line": 114,
                                                "content": "/* 初始化变量 */",
                                                "kind": "comment",
                                            }
                                        ],
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )

    assert parsed.findings[0].fixed_snippet[0].kind.value == "context"


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
            settings=Settings(
                _env_file=None,
                allow_insecure_defaults=True,
                model_max_tokens=2048,
            ),
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
            settings=Settings(model_structured_outputs_enabled=True, allow_insecure_defaults=True),
        )
    )

    response_format = captured["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "c_review_response"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["properties"]["findings"]["maxItems"] == 5


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
        "findings": [finding for _ in range(6)],
    }

    with pytest.raises(ModelInvocationError) as raised:
        _parse_response({"choices": [{"message": {"content": json.dumps(content)}}]})

    assert "invalid structured response" in str(raised.value)
    assert "at most 5 items" in (raised.value.details or "")


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


def test_chunk_file_preserves_original_line_numbers():
    chunks = _chunk_file(
        ReviewFile(
            relative_path="large.c",
            source_text="\n".join(f"int value_{index};" for index in range(1, 8)),
            size_bytes=120,
        ),
        max_chars=45,
    )

    assert len(chunks) > 1
    assert chunks[0].source_text.startswith("000001: int value_1;")
    assert chunks[1].source_text.startswith(f"{chunks[1].start_line:06d}:")
    assert chunks[-1].end_line == 7


def test_chunk_review_files_does_not_reject_large_batches():
    files = [
        ReviewFile(
            relative_path=f"file_{index}.c",
            source_text="int main(void) { return 0; }\n" * 4,
            size_bytes=120,
        )
        for index in range(12)
    ]
    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=1000,
        model_chunk_max_count=2,
    )

    chunks = _chunk_review_files(files, settings)

    assert len(chunks) > settings.model_chunk_max_count
    assert {chunk.relative_path for chunk in chunks} == {file.relative_path for file in files}


def test_chunk_review_batches_groups_small_files():
    files = [
        ReviewFile(
            relative_path=f"small_{index}.c",
            source_text="int value;\n" * 8,
            size_bytes=88,
        )
        for index in range(4)
    ]
    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=1000,
    )

    batches = _chunk_review_batches(files, settings)

    assert len(batches) < len(_chunk_review_files(files, settings))
    assert [chunk.relative_path for batch in batches for chunk in batch] == [
        file.relative_path for file in files
    ]


def test_chunk_review_batches_uses_conservative_context_budget():
    files = [
        ReviewFile(
            relative_path=f"small_{index}.c",
            source_text="int value;\n" * 80,
            size_bytes=880,
        )
        for index in range(3)
    ]
    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=12000,
    )

    batches = _chunk_review_batches(files, settings)
    effective_budget = _effective_chunk_max_chars(settings)

    assert effective_budget == 5400
    assert all(
        sum(len(f"===== FILE: {chunk.relative_path} =====\n{chunk.source_text}\n\n") for chunk in batch)
        <= effective_budget
        for batch in batches
    )


def test_chunked_review_halves_chunk_size_after_context_error(monkeypatch):
    seen_chunk_sizes: list[int] = []

    async def fake_invoke_model(*, files, settings, **_kwargs):
        seen_chunk_sizes.append(settings.model_chunk_max_chars)
        if settings.model_chunk_max_chars == 12000:
            raise ModelInvocationError("model context window is too small for this review request")
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)

    result = asyncio.run(
        _invoke_chunked_review(
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[
                ReviewFile(
                    relative_path="large.c",
                    source_text="int value;\n" * 300,
                    size_bytes=3000,
                )
            ],
            prompt="review",
            settings=Settings(
                _env_file=None,
                allow_insecure_defaults=True,
                model_chunk_max_chars=12000,
            ),
        )
    )

    assert result.score == 100
    assert result.findings == []
    assert 12000 in seen_chunk_sizes
    assert 6000 in seen_chunk_sizes


def test_merge_chunk_results_keeps_highest_priority_findings():
    finding = {
        "category": "memory_safety",
        "description": "description",
        "file_path": "main.c",
        "line": 1,
        "remediation": "remediation",
        "code_snippet": [],
        "fixed_snippet": [],
    }
    low_result = ModelReviewResponse.model_validate(
        {
            "summary": "low",
            "score": 90,
            "findings": [
                {**finding, "severity": "low", "title": f"low-{index}", "line": index}
                for index in range(1, 6)
            ],
        }
    )
    high_result = ModelReviewResponse.model_validate(
        {
            "summary": "high",
            "score": 40,
            "findings": [{**finding, "severity": "high", "title": "high", "line": 99}],
        }
    )

    merged = _merge_chunk_results([low_result, high_result])

    assert merged.score == 40
    assert len(merged.findings) == 5
    assert merged.findings[0].severity.value == "high"
    assert merged.findings[0].title == "high"


def test_invoke_selected_model_keeps_chunking_on_retry_instruction(monkeypatch, db_session_factory):
    from app.core.security import hash_password
    from app.db.models import ModelNode, ReviewFile, ReviewTask, User

    calls: list[tuple[int, str | None]] = []

    async def fake_invoke_model(*, files, retry_instruction=None, **_kwargs):
        calls.append((len(files), retry_instruction))
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router.get_settings", lambda: Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=1000,
        model_chunk_max_count=20,
    ))

    with db_session_factory() as db:
        user = User(username="chunker", password_hash=hash_password("chunker-password"))
        node = ModelNode(
            display_name="Review node",
            model_identifier="review-model",
            base_url="http://model-node",
            is_enabled=True,
        )
        task = ReviewTask(
            owner=user,
            model_node=node,
            input_mode="text",
            display_name="large.c",
            file_count=1,
            check_types=["logic"],
        )
        task.files.append(
            ReviewFile(
                relative_path="large.c",
                source_text="\n".join(f"int value_{index};" for index in range(100)),
                size_bytes=1600,
            )
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id, retry_instruction="previous chunk failed"))

    assert result.summary.startswith("分片审查完成")
    assert len(calls) > 1
    assert all(file_count == 1 for file_count, _ in calls)
    assert all(retry_instruction == "previous chunk failed" for _, retry_instruction in calls)

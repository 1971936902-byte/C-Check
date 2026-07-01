import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.db.models import ModelNode, ReviewFile
from app.core.config import Settings
from app.schemas.model_response import (
    COMPACT_MAX_FINDINGS,
    FindingCategory,
    FormattedFindingsResponse,
    ModelReviewResponse,
    ReviewFinding,
)
from app.services.model_router import (
    CANDIDATE_FORMAT_CONTRACT,
    CandidateCategoryDecision,
    CandidateCategoryDecisionResponse,
    ModelNodeDispatchPool,
    ModelInvocationError,
    ModelInvocationResult,
    RESPONSE_CONTRACT,
    _allowed_final_categories,
    _candidate_jsonl,
    _dedupe_candidate_findings,
    _partition_category_candidates,
    _resolve_unmatched_categories,
    _filter_candidate_findings,
    _parse_candidate_jsonl_response,
    _refine_candidate_line,
    _chunk_file,
    _effective_chunk_max_chars,
    _ensure_input_budget,
    _invoke_chunked_review,
    _chunk_review_batches,
    _chunk_review_files,
    _merge_chunk_results,
    _parse_response,
    _parse_typed_response,
    invoke_selected_model,
    invoke_model,
)
from app.services.static_c_rules import detect_static_c_findings


def test_parse_candidate_jsonl_keeps_complete_rows_before_truncated_tail():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"p":"src/main.c","l":7,"s":"high","t":"heap_oob_write","d":"写入超过分配边界"}\n'
                        '{"p":"src/main.c","l":9,"s":"medium","t":"resource_leak"'
                    )
                }
            }
        ]
    }

    parsed = _parse_candidate_jsonl_response(payload)

    assert len(parsed.findings) == 1
    assert parsed.findings[0].file_path == "src/main.c"
    assert parsed.findings[0].line == 7
    assert parsed.findings[0].category == FindingCategory.BUFFER_OVERFLOW


def test_parse_candidate_jsonl_accepts_legacy_findings_object_and_empty_list():
    legacy = _parse_candidate_jsonl_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "legacy",
                                "score": 80,
                                "findings": [
                                    {
                                        "file_path": "main.c",
                                        "line": 3,
                                        "severity": "medium",
                                        "category": "double_free",
                                        "description": "同一指针被重复释放",
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
    empty = _parse_candidate_jsonl_response({"choices": [{"message": {"content": "[]"}}]})

    assert legacy.findings[0].category == FindingCategory.MEMORY_SAFETY
    assert empty.findings == []


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


def test_parse_typed_response_accepts_findings_only_contract():
    parsed = _parse_typed_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "findings": [
                                    {
                                        "severity": "high",
                                        "category": "buffer_overflow",
                                        "title": "越界写入",
                                        "description": "写入长度超过目标缓冲区。",
                                        "file_path": "main.c",
                                        "line": 12,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        },
        response_model=FormattedFindingsResponse,
    )

    assert len(parsed.findings) == 1
    assert parsed.findings[0].file_path == "main.c"
    assert parsed.findings[0].line == 12


def test_parse_response_ignores_legacy_snippet_fields():
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

    assert parsed.findings[0].line == 114
    assert "fixed_snippet" not in parsed.findings[0].model_dump()


def test_parse_response_ignores_legacy_unknown_snippet_kind():
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

    assert parsed.findings[0].title == "补充说明"
    assert "fixed_snippet" not in parsed.findings[0].model_dump()


def test_parse_response_discards_legacy_overlong_snippets_before_schema_validation():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "fix snippet is too long",
                                "score": 80,
                                "findings": [
                                    {
                                        "severity": "medium",
                                        "category": "logic",
                                        "title": "long fix",
                                        "description": "model returned too many fixed snippet lines",
                                        "file_path": "src/main.c",
                                        "line": 92,
                                        "remediation": "keep the snippet short",
                                        "code_snippet": [],
                                        "fixed_snippet": [
                                            {"line": 92 + index, "content": f"line {index}", "kind": "context"}
                                            for index in range(9)
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

    assert parsed.findings[0].line == 92
    assert "fixed_snippet" not in parsed.findings[0].model_dump()


def test_parse_response_normalizes_model_category_aliases():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "model used non-schema category",
                                "score": 85,
                                "findings": [
                                    {
                                        "severity": "low",
                                        "category": "code_norm",
                                        "title": "duplicate code block",
                                        "description": "model used a code quality category alias",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 118,
                                        "evidence_ids": [],
                                        "call_chain": [],
                                        "confidence": 0.9,
                                        "remediation": "extract duplicate code into a helper",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
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

    assert parsed.findings[0].category.value == "maintainability"


def test_parse_response_normalizes_type_safety_category_aliases():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "model used type category aliases",
                                "score": 85,
                                "findings": [
                                    {
                                        "severity": "low",
                                        "category": "type_safety",
                                        "title": "type mismatch",
                                        "description": "model used a non-schema type safety category",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 118,
                                        "evidence_ids": [],
                                        "call_chain": [],
                                        "confidence": 0.9,
                                        "remediation": "use compatible types",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
                                    },
                                    {
                                        "severity": "medium",
                                        "category": "type_conversion",
                                        "title": "implicit conversion",
                                        "description": "model used a non-schema type conversion category",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 120,
                                        "evidence_ids": [],
                                        "call_chain": [],
                                        "confidence": 0.8,
                                        "remediation": "check conversion range before casting",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
                                    },
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )

    assert parsed.findings[0].category.value == "compatibility"
    assert parsed.findings[1].category.value == "integer_safety"


def test_parse_response_sanitizes_malformed_but_usable_findings():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "",
                                "score": "105",
                                "findings": [
                                    {
                                        "severity": "critical",
                                        "category": "unknown_model_category",
                                        "title": "custom category",
                                        "description": "存在资源泄漏风险",
                                        "file_path": "",
                                        "line": "42",
                                        "evidence_ids": "E1",
                                        "call_chain": "init->open",
                                        "confidence": "95%",
                                        "remediation": "",
                                        "code_snippet": "handle = open(path);",
                                        "fixed_snippet": [
                                            {"line": None, "content": "close(handle);", "kind": "comment"},
                                            {"line": 43, "content": ""},
                                            123,
                                        ],
                                        "unexpected": "ignored",
                                    },
                                    {
                                        "severity": "strange",
                                        "category": "totally_new_category",
                                        "title": "",
                                        "description": "",
                                        "file_path": None,
                                        "line": None,
                                        "remediation": None,
                                        "code_snippet": [],
                                        "fixed_snippet": [],
                                    },
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )

    assert parsed.summary == "审查完成。"
    assert parsed.score == 100
    assert parsed.findings[0].severity.value == "high"
    assert parsed.findings[0].category.value == "resource_leak"
    assert parsed.findings[0].file_path == "unknown.c"
    assert parsed.findings[0].line == 42
    assert parsed.findings[1].severity.value == "low"
    assert parsed.findings[1].category.value == "other"
    assert parsed.findings[1].title == "模型发现的问题"


def test_parse_response_normalizes_pointer_category_and_drops_confidence_aliases():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "pointer category aliases",
                                "score": 85,
                                "findings": [
                                    {
                                        "severity": "medium",
                                        "category": "null_pointer",
                                        "title": "missing null check",
                                        "description": "model used a pointer category alias",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 110,
                                        "evidence_ids": ["E1", "E2"],
                                        "call_chain": [],
                                        "confidence": "high",
                                        "remediation": "check arguments before dereference",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
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

    finding = parsed.findings[0]
    assert finding.category.value == "pointer_safety"
    assert not hasattr(finding, "confidence")


def test_parse_response_normalizes_natural_language_categories():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "natural language categories",
                                "score": 85,
                                "findings": [
                                    {
                                        "severity": "low",
                                        "category": "代码规范与可维护性",
                                        "title": "duplicate block",
                                        "description": "model returned a natural-language maintainability category",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 110,
                                        "remediation": "extract a helper",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
                                    },
                                    {
                                        "severity": "low",
                                        "category": "性能隐患",
                                        "title": "resource pressure",
                                        "description": "model returned a natural-language performance category",
                                        "file_path": "ctest_mid/stm32f10x_can.c",
                                        "line": 120,
                                        "remediation": "reduce repeated work",
                                        "code_snippet": [],
                                        "fixed_snippet": [],
                                    },
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )

    assert parsed.findings[0].category.value == "maintainability"
    assert parsed.findings[1].category.value == "performance"


def test_parse_response_normalizes_chinese_enums_and_drops_model_snippets():
    parsed = _parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "发现多个问题",
                                "score": 85,
                                "findings": [
                                    {
                                        "severity": "高",
                                        "category": "整数溢出与类型转换",
                                        "title": "整数溢出",
                                        "description": "整数相加可能溢出，导致后续分配大小错误。",
                                        "file_path": "dvcp.c",
                                        "line": 54,
                                        "evidence_ids": ["E1", "bad"],
                                        "call_chain": [],
                                        "confidence": 90,
                                        "remediation": "分配前检查整数运算是否溢出。",
                                        "code_snippet": ["int size1 = img.width + img.height;"],
                                        "fixed_snippet": [],
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

    finding = parsed.findings[0]
    assert finding.severity.value == "high"
    assert finding.category.value == "integer_safety"
    assert not hasattr(finding, "confidence")
    assert not hasattr(finding, "evidence_ids")
    assert not hasattr(finding, "code_snippet")


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
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
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


def test_invoke_model_returns_finish_reason_when_requested(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"summary":"ok","score":100,"findings":[]}'},
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
            return FakeResponse()

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", FakeClient)
    result = asyncio.run(
        invoke_model(
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[ReviewFile(relative_path="main.c", source_text="int main(void){return 0;}", size_bytes=25)],
            prompt="review",
            settings=Settings(_env_file=None, allow_insecure_defaults=True),
            return_metadata=True,
        )
    )

    assert isinstance(result, ModelInvocationResult)
    assert result.finish_reason == "length"
    assert result.value.score == 100
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 30
    assert result.elapsed_seconds is not None and result.elapsed_seconds >= 0


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
    assert response_format["json_schema"]["name"] == "c_review_fast_response"
    assert response_format["json_schema"]["strict"] is True
    assert "category_buckets" not in response_format["json_schema"]["schema"]["properties"]
    assert response_format["json_schema"]["schema"]["properties"]["findings"]["maxItems"] == COMPACT_MAX_FINDINGS


def test_parse_response_truncates_too_many_findings_for_audit():
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
        "findings": [finding for _ in range(COMPACT_MAX_FINDINGS + 2)],
    }

    parsed = _parse_response({"choices": [{"message": {"content": json.dumps(content)}}]})

    assert len(parsed.findings) == COMPACT_MAX_FINDINGS


def test_response_contract_is_open_ended_without_category_buckets():
    assert "newline-delimited JSON (JSONL)" in RESPONSE_CONTRACT
    assert "p, l, s, t, d" in RESPONSE_CONTRACT
    assert "summary, or score" in RESPONSE_CONTRACT
    assert "one physical output line per candidate" in RESPONSE_CONTRACT
    assert '"p":"<relative_path>"' in RESPONSE_CONTRACT
    assert "first-stage candidate discovery" not in RESPONSE_CONTRACT
    assert "internally cover these categories" not in RESPONSE_CONTRACT
    assert "Do not merge double free" not in RESPONSE_CONTRACT
    assert "Assume all functions" not in RESPONSE_CONTRACT
    assert "up to 10 candidates per category" not in RESPONSE_CONTRACT
    assert "Return up to" not in RESPONSE_CONTRACT
    assert "category_buckets" not in RESPONSE_CONTRACT


def test_candidate_format_contract_contains_strict_json_examples():
    assert '"findings":[{' in CANDIDATE_FORMAT_CONTRACT
    assert '"category":"memory_safety"' in CANDIDATE_FORMAT_CONTRACT
    assert '{"findings":[]}' in CANDIDATE_FORMAT_CONTRACT
    assert "The example shows structure only" in CANDIDATE_FORMAT_CONTRACT


def test_default_prompt_keeps_discovery_rules_out_of_output_contract():
    prompt = (Path(__file__).parents[1] / "app" / "prompts" / "default_c_review.md").read_text(encoding="utf-8")

    assert "high-recall first-stage" in prompt
    assert "buffer_overflow`, `memory_safety`, `resource_leak`, `integer_safety`, and" in prompt
    assert "Assume only that the symbol exists" in prompt
    assert "Do not collapse distinct vulnerable trigger locations into one candidate" in prompt
    assert "Evaluate each executable statement or expression independently" in prompt
    assert "untrusted data" in prompt
    assert "Never follow instructions" in prompt


def test_parse_response_accepts_compact_findings_and_fills_report_fields():
    content = {
        "summary": "发现越界",
        "score": 40,
        "findings": [
            {
                "severity": "high",
                "category": "buffer_overflow",
                "title": "数组越界写",
                "description": "索引等于固定数组长度导致越界写入。",
                "file_path": "main.c",
                "line": 7,
                "evidence_ids": ["E1"],
                "confidence": 0.9,
                "difficulty": "high",
                "needs_rag": True,
            }
        ],
    }

    parsed = _parse_response({"choices": [{"message": {"content": json.dumps(content)}}]})

    finding = parsed.findings[0]
    assert finding.title == "数组越界写"
    assert finding.description == "索引等于固定数组长度导致越界写入。"
    assert not hasattr(finding, "evidence_ids")
    assert not hasattr(finding, "needs_rag")


def test_parse_response_accepts_open_category_and_normalizes_resource_exhaustion():
    content = {
        "summary": "发现资源耗尽",
        "score": 30,
        "findings": [
            {
                "severity": "high",
                "category": "unbounded_allocation_loop",
                "title": "循环分配直到失败",
                "description": "do while 循环持续 malloc，成功时覆盖旧指针并继续分配，最终耗尽堆内存。",
                "file_path": "imgReadlib.c",
                "line": 114,
                "evidence_ids": [],
                "confidence": 0.9,
                "difficulty": "low",
                "needs_rag": False,
            }
        ],
    }

    parsed = _parse_response({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    finding = parsed.findings[0]
    assert finding.category.value == "resource_leak"
    assert finding.line == 114


def test_parse_response_falls_back_unknown_category_to_other():
    content = {
        "summary": "发现未知类型",
        "score": 70,
        "findings": [
            {
                "severity": "low",
                "category": "weird_model_label",
                "title": "无法归类的问题",
                "description": "模型发现真实风险但没有足够信息归入稳定类别。",
                "file_path": "main.c",
                "line": 3,
                "confidence": 0.5,
            }
        ],
    }

    parsed = _parse_response({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    finding = parsed.findings[0]
    assert finding.category.value == "other"


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
            settings=Settings(
                _env_file=None,
                allow_insecure_defaults=True,
                model_max_tokens=2048,
            ),
        )
    )

    assert result.summary == "ok"
    assert requested_tokens == [2048, 1444]


def test_finding_category_accepts_frontend_check_type_values():
    assert FindingCategory.BUFFER_OVERFLOW.value == "buffer_overflow"
    assert FindingCategory.INTEGER_SAFETY.value == "integer_safety"
    assert FindingCategory.MAINTAINABILITY.value == "maintainability"


def test_static_rules_cover_hard_sample_missed_patterns():
    source = ReviewFile(
        relative_path="hard_sample.c",
        source_text="""#include <stdint.h>
#include <stdlib.h>
#define STR_SAFE_COPY(dst, src, len) do{ memcpy(dst, src, len); }while(0)
#define CALC_PAGE_OFFSET(page, per) (page * per)
typedef struct { char *msg_content; } LogEntry;
int read_single_line(char* out_buf, uint32_t buf_max) {
    uint32_t idx = 0;
    out_buf[idx] = 'A';
    idx++;
    if (idx > buf_max) {
        return 0;
    }
    out_buf[idx] = '\\0';
    return 1;
}
void *page_mgr_create(uint32_t page_size) {
    return malloc(page_size * sizeof(LogEntry));
}
void load_log_page(uint32_t target_page) {
    uint64_t skip_bytes = CALC_PAGE_OFFSET(target_page, 100) * 128;
}
void batch_format_output(char *out_export, uint32_t export_max, char *temp_line) {
    uint32_t write_pos = 0;
    uint32_t line_len = strlen(temp_line);
    if (write_pos + line_len > export_max)
        return;
    strcpy(out_export + write_pos, temp_line);
}
""",
        size_bytes=800,
    )

    findings = detect_static_c_findings([source])
    titles = {finding.title for finding in findings}

    assert "宏参数缺少括号" in titles
    assert "分配尺寸乘法溢出" in titles
    assert "结束符二次越界" in titles
    assert "拷贝边界缺少等号" in titles


def test_candidate_filter_drops_stack_and_released_resource_leak_false_positives():
    source = ReviewFile(
        relative_path="leak_fp.c",
        source_text="""void free_string_array(char **arr, unsigned cnt);
void demo(void) {
    char export_buf[512];
    char* parts[4];
    free_string_array(parts, 4);
}
""",
        size_bytes=160,
    )
    findings = [
        ReviewFinding(
            severity="medium",
            category="resource_leak",
            title="误报栈缓冲区泄漏",
            description="export_buf 未释放",
            file_path="leak_fp.c",
            line=3,
        ),
        ReviewFinding(
            severity="medium",
            category="resource_leak",
            title="误报数组泄漏",
            description="parts 未释放",
            file_path="leak_fp.c",
            line=4,
        ),
    ]

    kept, rejected = _filter_candidate_findings([source], findings)

    assert kept == []
    assert rejected["resource_leak_false_positive"] == 2


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


def test_chunk_file_keeps_small_sources_whole_when_no_slice_threshold_is_set():
    source = ReviewFile(
        relative_path="small.c",
        source_text="\n".join(f"int value_{index};" for index in range(1, 10)),
        size_bytes=180,
    )

    chunks = _chunk_file(source, max_chars=45, no_slice_max_bytes=8 * 1024)

    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 9
    assert "000009: int value_9;" in chunks[0].source_text


def test_settings_default_to_fast_review_path():
    settings = Settings(_env_file=None, allow_insecure_defaults=True)

    assert settings.review_no_slice_max_bytes == 8 * 1024
    assert settings.model_max_tokens == 2048
    assert settings.model_chunk_max_chars == 18000
    assert settings.rag_on_demand_enabled is True
    assert settings.rag_review_units_enabled is False


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


def test_chunk_review_batches_can_isolate_files_for_node_balancing():
    files = [
        ReviewFile(
            relative_path=f"small_{index}.c",
            source_text="int value;\n" * 8,
            size_bytes=88,
        )
        for index in range(4)
    ]

    batches = _chunk_review_batches(
        files,
        Settings(_env_file=None, allow_insecure_defaults=True, model_chunk_max_chars=1000),
        isolate_chunks=True,
    )

    assert len(batches) == len(files)
    assert [[chunk.relative_path for chunk in batch] for batch in batches] == [
        [file.relative_path] for file in files
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

    assert effective_budget == 8400
    assert all(
        sum(len(f"===== FILE: {chunk.relative_path} =====\n{chunk.source_text}\n\n") for chunk in batch)
        <= effective_budget
        for batch in batches
    )


def test_chunk_review_batches_respects_prompt_aware_input_budget():
    files = [
        ReviewFile(
            relative_path="large.c",
            source_text="int value;\n" * 1200,
            size_bytes=12000,
        )
    ]
    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_context_window=4096,
        model_max_input_tokens=3000,
        model_max_tokens=512,
        model_token_chars_per_token=2,
        model_chunk_max_chars=20000,
    )

    batches = _chunk_review_batches(files, settings, prompt="review " * 200)
    effective_budget = _effective_chunk_max_chars(settings, prompt="review " * 200)

    assert effective_budget < settings.model_chunk_max_chars
    assert all(
        sum(len(f"===== FILE: {chunk.relative_path} =====\n{chunk.source_text}\n\n") for chunk in batch)
        <= effective_budget + 64
        for batch in batches
    )


def test_invoke_model_rejects_oversized_input_before_http(monkeypatch):
    class ForbiddenClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            raise AssertionError("HTTP client should not be opened for over-budget input")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("app.services.model_router.httpx.AsyncClient", ForbiddenClient)

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
                        relative_path="large.c",
                        source_text="int value;\n" * 500,
                        size_bytes=5000,
                    )
                ],
                prompt="review",
                settings=Settings(
                    _env_file=None,
                    allow_insecure_defaults=True,
                    model_context_window=2048,
                    model_max_input_tokens=1024,
                    model_max_tokens=1024,
                    model_token_chars_per_token=1,
                ),
            )
        )

    assert "context window is too small" in str(raised.value)
    assert "Estimated input tokens" in (raised.value.details or "")


def test_ensure_input_budget_allows_small_request():
    _ensure_input_budget(
        prompt="review",
        files=[ReviewFile(relative_path="main.c", source_text="int main(void) { return 0; }", size_bytes=28)],
        settings=Settings(_env_file=None, allow_insecure_defaults=True),
    )


def test_chunked_review_rejects_extreme_chunk_counts(monkeypatch):
    async def fake_invoke_model(**_kwargs):
        raise AssertionError("model should not be called when chunk count exceeds the safety limit")

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)

    with pytest.raises(ModelInvocationError) as raised:
        asyncio.run(
            _invoke_chunked_review(
                node=ModelNode(
                    display_name="test",
                    model_identifier="qwen-test",
                    base_url="http://model.local",
                    is_enabled=True,
                ),
                files=[
                    ReviewFile(
                        relative_path=f"file_{index}.c",
                        source_text="int value;\n" * 200,
                        size_bytes=2000,
                    )
                    for index in range(12)
                ],
                prompt="review",
                settings=Settings(
                    _env_file=None,
                    allow_insecure_defaults=True,
                    model_chunk_max_chars=1000,
                    model_chunk_max_count=2,
                    model_context_window=8192,
                    model_max_tokens=512,
                ),
            )
        )

    assert "too large to split safely" in str(raised.value)
    assert "MODEL_CHUNK_MAX_COUNT=2" in (raised.value.details or "")


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


def test_chunked_review_balances_batches_across_sibling_nodes(monkeypatch):
    calls: list[str] = []

    async def fake_invoke_model(*, node, **_kwargs):
        calls.append(node.base_url)
        await asyncio.sleep(0.01)
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    nodes = (
        ModelNode(
            id="node-0",
            display_name="GPU 0",
            model_identifier="qwen-test",
            base_url="http://gpu0",
            is_enabled=True,
            gpu_indices=[0],
        ),
        ModelNode(
            id="node-1",
            display_name="GPU 1",
            model_identifier="qwen-test",
            base_url="http://gpu1",
            is_enabled=True,
            gpu_indices=[1],
        ),
    )

    result = asyncio.run(
        _invoke_chunked_review(
            node=nodes[0],
            dispatch_pool=ModelNodeDispatchPool(nodes=nodes, base_loads={}),
            files=[
                ReviewFile(
                    relative_path=f"file_{index}.c",
                    source_text="int value;\n" * 4,
                    size_bytes=44,
                )
                for index in range(4)
            ],
            prompt="review",
            settings=Settings(
                _env_file=None,
                allow_insecure_defaults=True,
                model_chunk_max_chars=1000,
                model_chunk_concurrency=2,
                model_chunk_max_count=10,
            ),
        )
    )

    assert result.score == 100
    assert "http://gpu0" in calls
    assert "http://gpu1" in calls


def test_chunked_review_falls_back_when_a_node_is_unavailable(monkeypatch):
    calls: list[str] = []

    async def fake_invoke_model(*, node, **_kwargs):
        calls.append(node.base_url)
        if node.base_url == "http://gpu0":
            raise ModelInvocationError("selected model node is unavailable")
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    nodes = (
        ModelNode(
            id="node-0",
            display_name="GPU 0",
            model_identifier="qwen-test",
            base_url="http://gpu0",
            is_enabled=True,
            gpu_indices=[0],
        ),
        ModelNode(
            id="node-1",
            display_name="GPU 1",
            model_identifier="qwen-test",
            base_url="http://gpu1",
            is_enabled=True,
            gpu_indices=[1],
        ),
    )

    result = asyncio.run(
        _invoke_chunked_review(
            node=nodes[0],
            dispatch_pool=ModelNodeDispatchPool(nodes=nodes, base_loads={}),
            files=[ReviewFile(relative_path="main.c", source_text="int main(void){return 0;}", size_bytes=25)],
            prompt="review",
            settings=Settings(
                _env_file=None,
                allow_insecure_defaults=True,
                model_chunk_max_chars=1000,
                model_chunk_concurrency=1,
            ),
        )
    )

    assert result.score == 100
    assert calls == ["http://gpu0", "http://gpu1"]


def test_merge_chunk_results_keeps_all_sorted_findings():
    finding = {
        "category": "memory_safety",
        "description": "description",
        "file_path": "main.c",
        "line": 1,
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
    assert len(merged.findings) == 6
    assert merged.findings[0].severity.value == "high"
    assert merged.findings[0].title == "high"
    assert [finding.title for finding in merged.findings[1:]] == [f"low-{index}" for index in range(1, 6)]


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
        review_no_slice_max_bytes=1024,
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

    assert result.summary.startswith("两阶段审查完成")
    assert len(calls) > 1
    assert all(file_count == 1 for file_count, _ in calls)
    assert all(retry_instruction == "previous chunk failed" for _, retry_instruction in calls)


def test_invoke_selected_model_distributes_multi_file_task_across_sibling_nodes(monkeypatch, db_session_factory):
    from app.core.security import hash_password
    from app.db.models import ModelNode, ReviewFile, ReviewTask, User

    calls: list[str] = []

    async def fake_invoke_model(*, node, files, **_kwargs):
        calls.append(node.base_url)
        await asyncio.sleep(0.01)
        assert len(files) == 1
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router.get_settings", lambda: Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=1000,
        model_chunk_max_count=20,
        model_chunk_concurrency=2,
    ))

    with db_session_factory() as db:
        user = User(username="balancer", password_hash=hash_password("balancer-password"))
        node0 = ModelNode(
            display_name="GPU 0",
            model_identifier="review-model",
            base_url="http://gpu0",
            is_enabled=True,
            gpu_indices=[0],
        )
        node1 = ModelNode(
            display_name="GPU 1",
            model_identifier="review-model",
            base_url="http://gpu1",
            is_enabled=True,
            gpu_indices=[1],
        )
        task = ReviewTask(
            owner=user,
            model_node=node0,
            input_mode="folder",
            display_name="project",
            file_count=4,
            check_types=["logic"],
        )
        for index in range(4):
            task.files.append(
                ReviewFile(
                    relative_path=f"src/file_{index}.c",
                    source_text="int value;\n" * 4,
                    size_bytes=44,
                )
            )
        db.add_all([node1, task])
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert result.score == 100
    assert "http://gpu0" in calls
    assert "http://gpu1" in calls


def test_invoke_selected_model_keeps_large_task_off_reserved_small_node(monkeypatch, db_session_factory):
    from app.core.security import hash_password
    from app.db.models import ModelNode, ReviewFile, ReviewTask, User

    calls: list[str] = []

    async def fake_invoke_model(*, node, files, **_kwargs):
        calls.append(node.base_url)
        await asyncio.sleep(0.01)
        assert len(files) == 1
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router.get_settings", lambda: Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        model_chunk_max_chars=1000,
        model_chunk_max_count=20,
        model_chunk_concurrency=2,
        model_small_task_max_files=2,
        model_small_task_reserved_nodes=1,
        model_large_task_max_nodes=2,
    ))

    with db_session_factory() as db:
        user = User(username="large-runner", password_hash=hash_password("large-runner-password"))
        nodes = [
            ModelNode(
                display_name=f"GPU {index}",
                model_identifier="review-model",
                base_url=f"http://gpu{index}",
                is_enabled=True,
                gpu_indices=[index],
            )
            for index in range(3)
        ]
        task = ReviewTask(
            owner=user,
            model_node=nodes[0],
            input_mode="folder",
            display_name="large-project",
            file_count=4,
            check_types=["logic"],
        )
        for index in range(4):
            task.files.append(
                ReviewFile(
                    relative_path=f"src/file_{index}.c",
                    source_text="int value;\n" * 4,
                    size_bytes=44,
                )
            )
        db.add_all([*nodes[1:], task])
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert result.score == 100
    assert set(calls) == {"http://gpu0", "http://gpu1"}
    assert "http://gpu2" not in calls


def test_invoke_selected_model_caches_jsonl_and_formats_strict_final_report(monkeypatch, db_session_factory):
    from app.core.security import hash_password
    from app.db.models import ModelNode, ReviewFile, ReviewTask, User
    from app.schemas.model_response import ReviewFinding

    calls: list[dict] = []

    async def fake_invoke_model(*, prompt, settings, input_message=None, files=(), **_kwargs):
        calls.append(
            {
                "prompt": prompt,
                "input_message": input_message,
                "files": files,
                "model_max_tokens": settings.model_max_tokens,
            }
        )
        if input_message is not None:
            return FormattedFindingsResponse(
                findings=[
                    {
                        "severity": "high",
                        "category": "memory_safety",
                        "title": "释放后继续使用",
                        "description": "free 后继续写入同一指针。",
                        "file_path": "main.c",
                        "line": 2,
                    }
                ]
            )
        return ModelReviewResponse(
            summary="candidates",
            score=50,
            findings=[
                ReviewFinding(
                    severity="high",
                    category="memory_safety",
                    title="释放后继续使用",
                    description="free 后继续写入同一指针。",
                    file_path="main.c",
                    line=2,
                )
            ],
        )

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr(
        "app.services.model_router.get_settings",
        lambda: Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            rag_enabled=False,
            rag_candidate_scan_enabled=True,
            model_max_tokens=1024,
            candidate_model_max_tokens=2048,
            candidate_dynamic_tokens_enabled=False,
        ),
    )

    with db_session_factory() as db:
        user = User(username="candidate-runner", password_hash=hash_password("candidate-runner-password"))
        node = ModelNode(
            display_name="GPU",
            model_identifier="review-model",
            base_url="http://gpu0",
            is_enabled=True,
        )
        task = ReviewTask(
            owner=user,
            model_node=node,
            input_mode="text",
            display_name="candidate-project",
            file_count=1,
            check_types=["memory_safety"],
        )
        task.files.append(
            ReviewFile(
                relative_path="main.c",
                source_text="int main(void) {\n  free(p);\n  p[0] = 1;\n}\n",
                size_bytes=45,
            )
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert len(calls) == 2
    assert result.summary == "两阶段审查完成，共输出 1 个问题。"
    assert len(result.findings) == 1
    assert result.findings[0].line == 3
    assert calls[0]["input_message"] is None
    assert calls[0]["model_max_tokens"] == 2048
    assert calls[1]["files"] == ()
    assert calls[1]["model_max_tokens"] == 1024
    assert "CANDIDATE JSONL DOCUMENT" in calls[1]["input_message"]
    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.candidate_jsonl
        assert '"p":"main.c"' in task.candidate_jsonl


def test_candidate_stage_uses_dynamic_output_budget():
    from app.services.model_router import _candidate_stage_settings

    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        candidate_model_max_tokens=2048,
        candidate_dynamic_min_tokens=768,
        candidate_dynamic_base_tokens=384,
        candidate_dynamic_tokens_per_line=4.5,
    )
    small = [ReviewFile(relative_path="small.c", source_text="int main(void) { return 0; }\n", size_bytes=29)]
    large = [ReviewFile(relative_path="large.c", source_text="int value;\n" * 1000, size_bytes=11000)]

    assert _candidate_stage_settings(settings, small).model_max_tokens == 768
    assert _candidate_stage_settings(settings, large).model_max_tokens == 2048


def test_candidate_stage_increases_budget_for_risky_code_of_equal_size():
    from app.services.model_router import _candidate_stage_settings

    settings = Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        candidate_model_max_tokens=2048,
        candidate_dynamic_min_tokens=256,
        candidate_dynamic_base_tokens=200,
        candidate_dynamic_tokens_per_line=1,
        candidate_dynamic_tokens_per_function=24,
        candidate_dynamic_tokens_per_dangerous_op=24,
        candidate_dynamic_tokens_per_pointer_op=4,
    )
    safe_source = "\n".join(f"int value_{index};" for index in range(40))
    risky_source = "\n".join(
        [
            "int copy(char *dst, const char *src, int size) {",
            "  memcpy(dst, src, size);",
            "  return dst[size];",
            "}",
            *[f"int value_{index};" for index in range(36)],
        ]
    )
    safe = [ReviewFile(relative_path="safe.c", source_text=safe_source, size_bytes=len(safe_source))]
    risky = [ReviewFile(relative_path="risky.c", source_text=risky_source, size_bytes=len(risky_source))]

    safe_budget = _candidate_stage_settings(settings, safe).model_max_tokens
    risky_budget = _candidate_stage_settings(settings, risky).model_max_tokens
    assert risky_budget > safe_budget
    assert risky_budget <= settings.candidate_model_max_tokens


def test_candidate_stage_continues_once_after_length_truncation(monkeypatch):
    from app.services.model_router import _invoke_candidate_review

    calls: list[dict] = []
    first = ModelReviewResponse(
        summary="first",
        score=70,
        findings=[
            ReviewFinding(
                severity="high",
                category="memory_safety",
                title="越界写入",
                description="首次候选",
                file_path="main.c",
                line=3,
            )
        ],
    )
    second = ModelReviewResponse(
        summary="second",
        score=80,
        findings=[
            ReviewFinding(
                severity="medium",
                category="resource_leak",
                title="资源泄漏",
                description="续审候选",
                file_path="main.c",
                line=5,
            )
        ],
    )

    async def fake_invoke_model(**kwargs):
        calls.append(kwargs)
        return ModelInvocationResult(value=first, finish_reason="length") if len(calls) == 1 else second

    async def fake_finalize_candidate_review(**kwargs):
        return kwargs["candidate_result"]

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router._rag_context", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("app.services.model_router._finalize_candidate_review", fake_finalize_candidate_review)
    result = asyncio.run(
        _invoke_candidate_review(
            db=object(),
            task=object(),
            node=ModelNode(
                display_name="test",
                model_identifier="qwen-test",
                base_url="http://model.local",
                is_enabled=True,
            ),
            files=[ReviewFile(relative_path="main.c", source_text="int main(void) { return 0; }", size_bytes=29)],
            prompt="review",
            settings=Settings(_env_file=None, allow_insecure_defaults=True),
            retry_instruction=None,
        )
    )

    assert len(calls) == 2
    assert "CONTINUATION REQUEST" in calls[1]["user_context"]
    assert {(finding.line, finding.title) for finding in result.findings} == {(3, "越界写入"), (5, "资源泄漏")}


def test_refine_candidate_line_prefers_exact_mechanism_anchor():
    from app.schemas.model_response import ReviewFinding

    finding = ReviewFinding(
        severity="high",
        category="integer_safety",
        title="整数下溢",
        description="size2 由 img.width - img.height + 100 计算，可能发生下溢。",
        file_path="imgRead.c",
        line=45,
    )
    files = [
        ReviewFile(
            relative_path="imgRead.c",
            source_text="\n".join(
                [
                    "int main(void)",
                    "{",
                    "  // unrelated",
                    "  //{",
                    "  int size2 = img.width - img.height + 100;",
                    "  char* buff2 = (char*)malloc(size2);",
                    "}",
                ]
            ),
            size_bytes=128,
        )
    ]

    assert _refine_candidate_line(files, finding) == 5


def test_refine_candidate_line_moves_comment_to_nearest_following_statement_without_global_drift():
    files = [
        ReviewFile(
            relative_path="main.c",
            source_text="\n".join(
                [
                    "void copy(char *dst, const char *src, int n) {",
                    "  // first unsafe copy",
                    "  memcpy(dst, src, n);",
                    "  consume(dst);",
                    "  // second unsafe copy",
                    "  memcpy(dst + n, src, n);",
                    "}",
                ]
            ),
            size_bytes=160,
        )
    ]
    first = ReviewFinding(
        severity="high",
        category="buffer_overflow",
        title="unsafe copy",
        description="copy may exceed destination bounds",
        file_path="main.c",
        line=2,
    )
    second = first.model_copy(update={"line": 5})

    assert _refine_candidate_line(files, first) == 3
    assert _refine_candidate_line(files, second) == 6


def test_candidate_dedupe_keeps_repeated_defects_at_distinct_lines():
    candidate = ReviewFinding(
        severity="high",
        category="buffer_overflow",
        title="unsafe copy",
        description="copy may exceed destination bounds",
        file_path="main.c",
        line=3,
    )

    deduped = _dedupe_candidate_findings(
        [candidate, candidate.model_copy(), candidate.model_copy(update={"line": 6})]
    )

    assert [finding.line for finding in deduped] == [3, 6]


def test_candidate_rule_filter_drops_compilation_only_and_generic_null_advice():
    files = [
        ReviewFile(
            relative_path="main.c",
            source_text=(
                "int run(void) {\n  external_call();\n  worker->value;\n"
                "  CAN->TSR = 1;\n  assert_param(IS_MODE(mode));\n  return 0;\n}\n"
            ),
            size_bytes=128,
        )
    ]
    candidates = [
        ReviewFinding(
            severity="medium",
            category="other",
            title="函数未定义",
            description="external_call 缺少定义。",
            file_path="main.c",
            line=2,
        ),
        ReviewFinding(
            severity="low",
            category="pointer_safety",
            title="缺少空指针检查",
            description="调用前未检查 worker 是否为空。",
            file_path="main.c",
            line=2,
        ),
        ReviewFinding(
            severity="high",
            category="pointer_safety",
            title="空指针解引用",
            description="worker 可为空时直接解引用 value。",
            file_path="main.c",
            line=3,
        ),
        ReviewFinding(
            severity="high",
            category="pointer_safety",
            title="CAN空指针",
            description="CAN可能为空并被解引用。",
            file_path="main.c",
            line=4,
        ),
        ReviewFinding(
            severity="medium",
            category="input_validation",
            title="缺少参数校验",
            description="IS_MODE参数缺少检查。",
            file_path="main.c",
            line=5,
        ),
    ]

    kept, rejected = _filter_candidate_findings(files, candidates)

    assert [finding.title for finding in kept] == ["空指针解引用"]
    assert rejected["compilation_only"] == 1
    assert rejected["generic_null_check"] == 1
    assert rejected["peripheral_null_pointer"] == 1
    assert rejected["vendor_assert_validation"] == 1


def test_candidate_rule_filter_keeps_extended_category_after_task_scope_selection():
    candidate = ReviewFinding(
        severity="low",
        category="performance",
        title="repeated full scan",
        description="the loop scans the complete buffer on every iteration",
        file_path="main.c",
        line=2,
    )
    files = [
        ReviewFile(
            relative_path="main.c",
            source_text="void run(void) {\n  scan_all_items();\n}\n",
            size_bytes=41,
        )
    ]

    kept, rejected = _filter_candidate_findings(files, [candidate])

    assert [finding.category for finding in kept] == [FindingCategory.PERFORMANCE]
    assert rejected["out_of_scope_category"] == 0


def test_candidate_jsonl_is_lightweight_and_allowed_categories_follow_task_selection():
    candidates = [
        ReviewFinding(
            severity="medium",
            category="logic",
            title="候选一",
            description="候选一描述",
            file_path="main.c",
            line=3,
        ),
        ReviewFinding(
            severity="low",
            category="other",
            title="候选二",
            description="候选二描述",
            file_path="main.c",
            line=5,
        ),
        ReviewFinding(
            severity="high",
            category="buffer_overflow",
            title="候选三",
            description="候选三描述",
            file_path="main.c",
            line=8,
        ),
    ]
    jsonl = _candidate_jsonl(candidates)

    assert len(jsonl.splitlines()) == 3
    assert set(json.loads(jsonl.splitlines()[0])) == {"p", "l", "s", "c", "t", "d"}
    assert json.loads(jsonl.splitlines()[0])["c"] == "logic"
    reparsed = _parse_candidate_jsonl_response({"choices": [{"message": {"content": jsonl}}]})
    assert reparsed.findings[0].category == FindingCategory.LOGIC
    assert reparsed.findings[0].title == candidates[0].title
    assert _allowed_final_categories(["logic", "buffer_overflow"]) == ("logic", "buffer_overflow")
    assert _allowed_final_categories(["performance"]) == ("performance",)


def test_category_partition_keeps_exact_matches_and_routes_other_for_semantic_review():
    matched = ReviewFinding(
        severity="medium",
        category="logic",
        title="wrong_state",
        description="state update is incorrect",
        file_path="main.c",
        line=2,
    )
    unresolved = ReviewFinding(
        severity="low",
        category="other",
        title="slow_loop",
        description="loop repeatedly scans the same buffer",
        file_path="main.c",
        line=4,
    )

    deterministic, semantic = _partition_category_candidates([matched, unresolved], ["logic", "performance"])

    assert deterministic == [matched]
    assert semantic == [unresolved]


def test_unmatched_category_is_corrected_by_semantic_fallback_without_changing_facts(monkeypatch):
    calls: list[dict] = []

    async def fake_invoke_model(**kwargs):
        calls.append(kwargs)
        return CandidateCategoryDecisionResponse(
            decisions=[CandidateCategoryDecision(i=0, a="correct", c="performance")]
        )

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    node = ModelNode(display_name="GPU", model_identifier="review-model", base_url="http://gpu0")
    task = type("Task", (), {"model_log": None})()
    files = [ReviewFile(relative_path="main.c", source_text="int run(void) { return slow_call(); }\n", size_bytes=39)]
    candidate = ReviewFinding(
        severity="low",
        category="other",
        title="slow_loop",
        description="repeated full-buffer scan",
        file_path="main.c",
        line=1,
    )

    resolved, failed = asyncio.run(
        _resolve_unmatched_categories(
            task=task,
            node=node,
            files=files,
            findings=[candidate],
            allowed_categories=["performance"],
            settings=Settings(_env_file=None, allow_insecure_defaults=True),
        )
    )

    assert failed == 0
    assert len(calls) == 1
    assert calls[0]["files"] == ()
    assert calls[0]["settings"].model_max_tokens == 128
    assert resolved[0].category == FindingCategory.PERFORMANCE
    assert resolved[0].file_path == candidate.file_path
    assert resolved[0].line == candidate.line
    assert resolved[0].description == candidate.description


def test_semantic_fallback_deduplicates_identical_inputs_before_dispatch(monkeypatch):
    calls: list[dict] = []

    async def fake_invoke_model(**kwargs):
        calls.append(kwargs)
        return CandidateCategoryDecisionResponse(
            decisions=[CandidateCategoryDecision(i=0, a="correct", c="performance")]
        )

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    task = type("Task", (), {"model_log": None})()
    candidate = ReviewFinding(
        severity="low",
        category="other",
        title="slow loop",
        description="repeated full-buffer scan",
        file_path="main.c",
        line=2,
    )
    resolved, failed = asyncio.run(
        _resolve_unmatched_categories(
            task=task,
            node=ModelNode(display_name="GPU", model_identifier="review-model", base_url="http://gpu0"),
            files=[ReviewFile(relative_path="main.c", source_text="void run(void) {\n  scan();\n}\n", size_bytes=32)],
            findings=[candidate, candidate.model_copy()],
            allowed_categories=["performance"],
            settings=Settings(_env_file=None, allow_insecure_defaults=True),
        )
    )

    assert failed == 0
    assert len(calls) == 1
    assert calls[0]["input_message"].count('"i":') == 1
    assert len(resolved) == 1


def test_semantic_category_failure_drops_only_unresolved_candidates(monkeypatch):
    async def failing_invoke_model(**_kwargs):
        raise ModelInvocationError("offline")

    monkeypatch.setattr("app.services.model_router.invoke_model", failing_invoke_model)
    task = type("Task", (), {"model_log": None})()
    candidate = ReviewFinding(
        severity="low",
        category="other",
        title="ambiguous",
        description="uncertain category",
        file_path="main.c",
        line=1,
    )

    resolved, failed = asyncio.run(
        _resolve_unmatched_categories(
            task=task,
            node=ModelNode(display_name="GPU", model_identifier="review-model", base_url="http://gpu0"),
            files=[ReviewFile(relative_path="main.c", source_text="int value;\n", size_bytes=11)],
            findings=[candidate],
            allowed_categories=["performance"],
            settings=Settings(_env_file=None, allow_insecure_defaults=True),
        )
    )

    assert resolved == []
    assert failed == 1
    assert "unresolved candidates dropped" in task.model_log


def test_candidate_pipeline_uses_rag_only_for_first_stage_and_jsonl_only_for_second_stage(
    monkeypatch,
    db_session_factory,
):
    from app.core.security import hash_password
    from app.db.models import ModelNode, ReviewTask, User

    calls: list[dict] = []
    rag_purposes: list[str] = []

    async def fake_invoke_model(*, prompt, input_message=None, user_context=None, files=(), **_kwargs):
        calls.append(
            {"prompt": prompt, "input_message": input_message, "user_context": user_context, "files": files}
        )
        if input_message is not None:
            return FormattedFindingsResponse(
                findings=[
                    {
                        "severity": "medium",
                        "category": "logic",
                        "title": "外部状态错误",
                        "description": "结果取决于外部函数返回值。",
                        "file_path": "main.c",
                        "line": 2,
                    }
                ]
            )
        return ModelReviewResponse(
            summary="candidate",
            score=80,
            findings=[
                ReviewFinding(
                    severity="medium",
                    category="logic",
                    title="外部状态错误",
                    description="结果取决于外部函数返回值。",
                    file_path="main.c",
                    line=2,
                )
            ],
        )

    def fake_rag_context(_db, _task, _settings, *, purpose, **_kwargs):
        rag_purposes.append(purpose)
        return "DEFINITION CONTEXT TEST"

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router._rag_context", fake_rag_context)
    monkeypatch.setattr(
        "app.services.model_router.get_settings",
        lambda: Settings(_env_file=None, allow_insecure_defaults=True, rag_enabled=True),
    )

    with db_session_factory() as db:
        user = User(username="rag-stage-runner", password_hash=hash_password("rag-stage-runner-password"))
        node = ModelNode(
            display_name="GPU",
            model_identifier="review-model",
            base_url="http://gpu0",
            is_enabled=True,
        )
        task = ReviewTask(
            owner=user,
            model_node=node,
            input_mode="text",
            display_name="candidate-rag",
            file_count=1,
            check_types=["logic"],
        )
        task.files.append(
            ReviewFile(
                relative_path="main.c",
                source_text="int run(void) {\n  return external_state();\n}\n",
                size_bytes=48,
            )
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert len(result.findings) == 1
    assert rag_purposes == ["candidate"]
    assert "DEFINITION CONTEXT TEST" not in calls[0]["prompt"]
    assert "DEFINITION CONTEXT TEST" in calls[0]["user_context"]
    assert "默认快速审查仅关注" not in calls[0]["prompt"]
    assert "DEFINITION CONTEXT TEST" not in calls[1]["prompt"]
    assert "ALLOWED FINAL CATEGORIES: logic" in calls[1]["prompt"]
    assert calls[1]["files"] == ()
    assert "CANDIDATE JSONL DOCUMENT" in calls[1]["input_message"]

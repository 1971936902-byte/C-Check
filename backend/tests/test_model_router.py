import pytest

from app.schemas.model_response import FindingCategory
from app.services.model_router import ModelInvocationError, _parse_response


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


def test_finding_category_accepts_frontend_check_type_values():
    assert FindingCategory.BUFFER_OVERFLOW.value == "buffer_overflow"
    assert FindingCategory.INTEGER_SAFETY.value == "integer_safety"
    assert FindingCategory.MAINTAINABILITY.value == "maintainability"

import pytest

from app.services.check_types import ALL_CHECK_TYPES, check_types_prompt, validate_check_types


def test_validate_check_types_accepts_selected_dimensions():
    selected = ["memory_safety", "logic", "portability"]
    assert validate_check_types(selected) == selected


def test_validate_check_types_rejects_empty_or_unknown_dimensions():
    with pytest.raises(ValueError, match="at least one"):
        validate_check_types([])
    with pytest.raises(ValueError, match="unsupported"):
        validate_check_types(["not-real"])


def test_check_types_prompt_lists_selected_dimensions():
    prompt = check_types_prompt(["memory_safety", "logic"])
    assert "内存安全" in prompt
    assert "逻辑错误" in prompt
    assert len(ALL_CHECK_TYPES) >= 10

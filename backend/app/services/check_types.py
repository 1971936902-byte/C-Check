from __future__ import annotations


CHECK_TYPE_LABELS = {
    "memory_safety": "内存安全",
    "buffer_overflow": "缓冲区溢出",
    "pointer_safety": "空指针与野指针",
    "resource_leak": "资源泄漏",
    "integer_safety": "整数溢出与类型转换",
    "logic": "逻辑错误",
}
ALL_CHECK_TYPES = list(CHECK_TYPE_LABELS)
DEFAULT_FAST_CHECK_TYPES = set(ALL_CHECK_TYPES)


def validate_check_types(check_types: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(check_types or []))
    if not selected:
        raise ValueError("at least one check type must be selected")
    unsupported = [item for item in selected if item not in CHECK_TYPE_LABELS]
    if unsupported:
        raise ValueError(f"unsupported check types: {', '.join(unsupported)}")
    return selected


def check_types_prompt(check_types: list[str]) -> str:
    selected = validate_check_types(check_types)
    labels = [CHECK_TYPE_LABELS[item] for item in selected]
    return "默认快速审查仅关注以下高价值 C 缺陷类型：\n" + "\n".join(f"- {label}" for label in labels)

from __future__ import annotations


CHECK_TYPE_LABELS = {
    "memory_safety": "释放后使用/内存破坏",
    "buffer_overflow": "缓冲区/数组越界",
    "pointer_safety": "野指针/悬空指针",
    "resource_leak": "资源泄漏",
    "integer_safety": "长度/索引整数风险",
    "logic": "严重状态机/协议逻辑",
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
    return (
        "默认快速审查仅关注以下高价值 C 缺陷类型；普通空指针未校验、未定义/未声明、"
        "未初始化猜测、普通返回值未检查和代码风格问题不作为高级安全问题：\n"
        + "\n".join(f"- {label}" for label in labels)
    )

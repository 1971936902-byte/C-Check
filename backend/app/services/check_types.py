from __future__ import annotations


CHECK_TYPE_LABELS = {
    "memory_safety": "内存安全",
    "buffer_overflow": "缓冲区溢出",
    "pointer_safety": "空指针与野指针",
    "resource_leak": "资源泄漏",
    "concurrency": "并发与线程安全",
    "logic": "逻辑错误",
    "input_validation": "输入校验",
    "integer_safety": "整数溢出与类型转换",
    "compatibility": "编译兼容性",
    "portability": "跨平台可移植性",
    "performance": "性能隐患",
    "maintainability": "代码规范与可维护性",
}
ALL_CHECK_TYPES = list(CHECK_TYPE_LABELS)


def validate_check_types(check_types: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(check_types or []))
    if not selected:
        raise ValueError("at least one check type must be selected")
    unsupported = [item for item in selected if item not in CHECK_TYPE_LABELS]
    if unsupported:
        raise ValueError(f"unsupported check types: {', '.join(unsupported)}")
    return selected


def check_types_prompt(check_types: list[str]) -> str:
    labels = [CHECK_TYPE_LABELS[item] for item in validate_check_types(check_types)]
    return "本次审查需要重点覆盖以下检查类型：\n" + "\n".join(f"- {label}" for label in labels)

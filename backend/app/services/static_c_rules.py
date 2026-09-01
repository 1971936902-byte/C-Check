from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.db.models import ReviewFile
from app.schemas.model_response import FindingCategory, FindingSeverity, ReviewFinding
from app.services.security_analysis import detect_protocol_length_buffer_findings


_FUNC_MACRO_RE = re.compile(r"^\s*#\s*define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)\)\s*(?P<body>.*)")
_ALLOC_SIZE_RE = re.compile(r"\b(?:malloc|realloc)\s*\((?P<size>[^;]+)\)")
_CALLOC_SIZE_RE = re.compile(r"\bcalloc\s*\((?P<count>[^,;]+),(?P<size>[^;]+)\)")
_UNSIGNED_INIT_RE = re.compile(
    r"\b(?:u?int(?:8|16|32|64)_t|size_t|uint32_t|uint64_t|unsigned\s+\w+)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<expression>[^;]+);"
)
_SIGNED_ACCUM_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*[A-Za-z_][A-Za-z0-9_]*\s*\*\s*(?:10|[A-Za-z_][A-Za-z0-9_]*)\s*[+-]")
_OFF_BY_ONE_GUARD_RE = re.compile(r"\bif\s*\(\s*(?P<index>[A-Za-z_][A-Za-z0-9_]*)\s*>\s*(?P<limit>[A-Za-z_][A-Za-z0-9_]*)\s*\)")
_NULL_TERMINATOR_WRITE_RE = re.compile(r"(?P<buffer>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<index>[A-Za-z_][A-Za-z0-9_]*)\s*\]\s*=\s*'\\0'")
_COPY_AFTER_GREATER_GUARD_RE = re.compile(
    r"\bif\s*\(\s*(?P<left>[A-Za-z_][A-Za-z0-9_]*)\s*\+\s*(?P<len>[A-Za-z_][A-Za-z0-9_]*)\s*>\s*(?P<limit>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
)
_UNSAFE_COPY_RE = re.compile(r"\b(?:strcpy|memcpy|memmove|sprintf)\s*\(")


@dataclass(frozen=True)
class _Guard:
    index: str
    limit: str
    line: int


def detect_static_c_findings(files: Sequence[ReviewFile]) -> list[ReviewFinding]:
    """Find deterministic C risk patterns that LLMs often miss.

    These rules are intentionally conservative. They generate supplemental
    candidates for issues that are cheap to prove from local syntax and were
    repeatedly missed in first-stage model scans.
    """

    findings: list[ReviewFinding] = []
    for source in files:
        lines = source.source_text.splitlines()
        findings.extend(_macro_findings(source, lines))
        findings.extend(_allocation_size_findings(source, lines))
        findings.extend(_integer_expression_findings(source, lines))
        findings.extend(_off_by_one_findings(source, lines))
        findings.extend(_copy_boundary_findings(source, lines))
    findings.extend(detect_protocol_length_buffer_findings(files))
    return findings


def _finding(
    source: ReviewFile,
    *,
    line: int,
    severity: FindingSeverity,
    category: FindingCategory,
    title: str,
    description: str,
) -> ReviewFinding:
    return ReviewFinding(
        severity=severity,
        category=category,
        title=title,
        description=description,
        file_path=source.relative_path,
        line=line,
    )


def _macro_findings(source: ReviewFile, lines: list[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for line_number, line in enumerate(lines, start=1):
        match = _FUNC_MACRO_RE.match(line)
        if not match:
            continue
        params = [item.strip() for item in match.group("params").split(",") if item.strip()]
        body = match.group("body").strip()
        if not params or not _macro_body_has_risky_operator(body):
            continue
        unwrapped = [param for param in params if _param_used_without_parentheses(body, param)]
        if not unwrapped:
            continue
        findings.append(
            _finding(
                source,
                line=line_number,
                severity=FindingSeverity.MEDIUM,
                category=FindingCategory.INTEGER_SAFETY,
                title="宏参数缺少括号",
                description=f"宏 {match.group('name')} 的参数 {', '.join(unwrapped[:3])} 在运算表达式中未加括号，调用处传入复合表达式时可能改变长度或偏移计算。",
            )
        )
    return findings


def _macro_body_has_risky_operator(body: str) -> bool:
    return any(operator in body for operator in ("+", "-", "*", "/", "<<", ">>", "memcpy", "memmove", "strcpy"))


def _param_used_without_parentheses(body: str, param: str) -> bool:
    for match in re.finditer(rf"\b{re.escape(param)}\b", body):
        before = body[: match.start()].rstrip()
        after = body[match.end() :].lstrip()
        if not before.endswith("(") or not after.startswith(")"):
            return True
    return False


def _allocation_size_findings(source: ReviewFile, lines: list[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for line_number, line in enumerate(lines, start=1):
        allocation = _ALLOC_SIZE_RE.search(line)
        calloc = _CALLOC_SIZE_RE.search(line)
        risky_size = allocation is not None and _has_binary_multiplication(allocation.group("size"))
        risky_calloc = calloc is not None and not _is_compile_time_constant(calloc.group("count"))
        if risky_size or risky_calloc:
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.INTEGER_SAFETY,
                    title="分配尺寸乘法溢出",
                    description="内存分配尺寸包含乘法表达式，缺少溢出上界校验时可能分配过小缓冲区并在后续写入中触发堆越界。",
                )
            )
    return findings


def _has_binary_multiplication(expression: str) -> bool:
    # Ignore pointer declarators inside sizeof(*ptr); they are not arithmetic.
    without_sizeof = re.sub(r"\bsizeof\s*\([^()]*\)", "1", expression)
    without_sizeof = re.sub(r"\bsizeof\s+[A-Za-z_][A-Za-z0-9_]*", "1", without_sizeof)
    return re.search(r"(?:\b[A-Za-z_][A-Za-z0-9_]*\b|\d+|\))\s*\*\s*(?:\b[A-Za-z_][A-Za-z0-9_]*\b|\d+|\()", without_sizeof) is not None


def _is_compile_time_constant(expression: str) -> bool:
    return re.fullmatch(r"\s*(?:\d+|sizeof\s*(?:\([^()]+\)|[A-Za-z_][A-Za-z0-9_]*))\s*", expression) is not None


def _integer_expression_findings(source: ReviewFile, lines: list[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for line_number, line in enumerate(lines, start=1):
        if _SIGNED_ACCUM_RE.search(line):
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.INTEGER_SAFETY,
                    title="有符号累乘溢出",
                    description="有符号整数在循环中累乘累加，未先检查上下界，极端输入会触发有符号整数溢出。",
                )
            )
        unsigned_init = _UNSIGNED_INIT_RE.search(line)
        if unsigned_init and re.search(r"-(?!>)", unsigned_init.group("expression")):
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.LOGIC,
                    title="差值溢出导致判断失效",
                    description="有符号差值直接赋给无符号变量，极值输入下可能先溢出再比较，导致后续范围判断失效。",
                )
            )
    return findings


def _off_by_one_findings(source: ReviewFile, lines: list[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    recent_guards: list[_Guard] = []
    for line_number, line in enumerate(lines, start=1):
        guard_match = _OFF_BY_ONE_GUARD_RE.search(line)
        if guard_match:
            recent_guards.append(_Guard(guard_match.group("index"), guard_match.group("limit"), line_number))
            if _index_is_used_for_array_access(lines, line_number, guard_match.group("index")):
                findings.append(
                    _finding(
                        source,
                        line=line_number,
                        severity=FindingSeverity.HIGH,
                        category=FindingCategory.BUFFER_OVERFLOW,
                        title="边界判断缺少等号",
                        description=f"{guard_match.group('index')} > {guard_match.group('limit')} 未覆盖等于上限的情况，边界长度输入可能越界写入。",
                    )
                )
        terminator_match = _NULL_TERMINATOR_WRITE_RE.search(line)
        if not terminator_match:
            continue
        index = terminator_match.group("index")
        if any(guard.index == index and line_number - guard.line <= 12 for guard in recent_guards):
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.BUFFER_OVERFLOW,
                    title="结束符二次越界",
                    description="循环边界允许索引等于缓冲区上限，退出后再写入字符串结束符会造成二次越界。",
                )
            )
    return findings


def _index_is_used_for_array_access(lines: list[str], line_number: int, index_name: str) -> bool:
    start = max(1, line_number - 8)
    end = min(len(lines), line_number + 8)
    pattern = re.compile(rf"\[[^\]]*\b{re.escape(index_name)}\b[^\]]*\]\s*(?:=|\+\+|--)")
    return any(pattern.search(lines[current - 1]) for current in range(start, end + 1))


def _copy_boundary_findings(source: ReviewFile, lines: list[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    guards: list[tuple[int, str, str]] = []
    for line_number, line in enumerate(lines, start=1):
        guard_match = _COPY_AFTER_GREATER_GUARD_RE.search(line)
        if guard_match:
            guards.append((line_number, guard_match.group("left"), guard_match.group("limit")))
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.BUFFER_OVERFLOW,
                    title="拷贝边界缺少等号",
                    description="累计写入长度只判断大于上限，等于上限时仍可能让后续字符串结束符写到缓冲区外。",
                )
            )
            continue
        if not _UNSAFE_COPY_RE.search(line):
            continue
        if any(line_number - guard_line <= 8 and (left in line or limit in line) for guard_line, left, limit in guards):
            findings.append(
                _finding(
                    source,
                    line=line_number,
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.BUFFER_OVERFLOW,
                    title="边界后不安全拷贝",
                    description="前置边界判断未覆盖等于上限的情况，随后执行不安全拷贝会写出目标缓冲区。",
                )
            )
    return findings

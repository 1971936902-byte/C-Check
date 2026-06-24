from __future__ import annotations

from typing import Any

from app.schemas.model_response import FindingCategory, FindingSeverity


MAX_MODEL_SNIPPET_LINES = 5
MAX_FINDINGS = 2000


def _as_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _truncate(value: str, limit: int, *, default: str) -> str:
    value = value.strip() or default
    return value[:limit]


def _as_line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, float) and value.is_integer():
        line = int(value)
        return line if line >= 1 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            line = int(stripped)
            return line if line >= 1 else None
    return None


def _as_score(value: Any) -> float:
    if isinstance(value, bool):
        return 80.0
    if isinstance(value, (int, float)):
        return max(0.0, min(100.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(100.0, float(value.strip().rstrip("%"))))
        except ValueError:
            return 80.0
    return 80.0


class ModelOutputSanitizer:
    """Convert loose model JSON into the strict review response schema."""

    severity_aliases = {
        "high": "high",
        "critical": "high",
        "severe": "high",
        "\u9ad8": "high",
        "\u9ad8\u5371": "high",
        "\u4e25\u91cd": "high",
        "medium": "medium",
        "moderate": "medium",
        "middle": "medium",
        "\u4e2d": "medium",
        "\u4e2d\u5371": "medium",
        "\u4e2d\u7b49": "medium",
        "low": "low",
        "minor": "low",
        "\u4f4e": "low",
        "\u4f4e\u5371": "low",
        "suggestion": "suggestion",
        "info": "suggestion",
        "informational": "suggestion",
        "hint": "suggestion",
        "notice": "suggestion",
        "\u5efa\u8bae": "suggestion",
        "\u63d0\u793a": "suggestion",
    }

    category_aliases = {
        "memory": "memory_safety",
        "memory_safety": "memory_safety",
        "memory safety": "memory_safety",
        "lifetime": "memory_safety",
        "double_free": "memory_safety",
        "double-free": "memory_safety",
        "use_after_free": "memory_safety",
        "use-after-free": "memory_safety",
        "uaf": "memory_safety",
        "buffer": "buffer_overflow",
        "bounds": "buffer_overflow",
        "buffer_overflow": "buffer_overflow",
        "buffer overflow": "buffer_overflow",
        "out_of_bounds": "buffer_overflow",
        "out-of-bounds": "buffer_overflow",
        "oob": "buffer_overflow",
        "pointer": "pointer_safety",
        "pointer_safety": "pointer_safety",
        "null_safety": "pointer_safety",
        "null_pointer": "pointer_safety",
        "null-pointer": "pointer_safety",
        "null_dereference": "pointer_safety",
        "null_pointer_dereference": "pointer_safety",
        "nullptr": "pointer_safety",
        "dangling_pointer": "pointer_safety",
        "wild_pointer": "pointer_safety",
        "invalid_pointer": "pointer_safety",
        "resource": "resource_leak",
        "resource_leak": "resource_leak",
        "resource leak": "resource_leak",
        "leak": "resource_leak",
        "memory_leak": "resource_leak",
        "logic": "logic",
        "logical": "logic",
        "robustness": "logic",
        "security": "security",
        "input_validation": "input_validation",
        "input validation": "input_validation",
        "param_check": "input_validation",
        "parameter_check": "input_validation",
        "parameter_validation": "input_validation",
        "argument_validation": "input_validation",
        "invalid_argument": "input_validation",
        "assertion": "input_validation",
        "integer": "integer_safety",
        "integer_safety": "integer_safety",
        "integer safety": "integer_safety",
        "overflow": "integer_safety",
        "integer_overflow": "integer_safety",
        "integer_underflow": "integer_safety",
        "type_conversion": "integer_safety",
        "implicit_cast": "integer_safety",
        "integer_conversion": "integer_safety",
        "cast_overflow": "integer_safety",
        "concurrency": "concurrency",
        "thread_safety": "concurrency",
        "race_condition": "concurrency",
        "performance": "performance",
        "perf": "performance",
        "style": "style",
        "code_style": "style",
        "coding_style": "style",
        "naming": "style",
        "maintainability": "maintainability",
        "code_norm": "maintainability",
        "code_quality": "maintainability",
        "code_smell": "maintainability",
        "duplication": "maintainability",
        "readability": "maintainability",
        "compatibility": "compatibility",
        "type_safety": "compatibility",
        "type safety": "compatibility",
        "type_mismatch": "compatibility",
        "type mismatch": "compatibility",
        "portability": "portability",
    }

    category_keywords = (
        (("\u53ef\u7ef4\u62a4", "\u7ef4\u62a4\u6027", "\u4ee3\u7801\u89c4\u8303", "\u4ee3\u7801\u8d28\u91cf", "\u91cd\u590d\u4ee3\u7801", "\u91cd\u590d\u5757"), "maintainability"),
        (("\u6027\u80fd", "\u6548\u7387", "\u8017\u65f6", "\u8d44\u6e90\u8017\u5c3d", "\u6808\u8017\u5c3d", "\u5806\u8017\u5c3d"), "performance"),
        (("\u7a7a\u6307\u9488", "\u91ce\u6307\u9488", "\u60ac\u7a7a\u6307\u9488", "null pointer", "null-pointer"), "pointer_safety"),
        (("\u8d8a\u754c", "\u7f13\u51b2\u533a", "buffer overflow", "out of bounds"), "buffer_overflow"),
        (("\u5185\u5b58\u6cc4\u6f0f", "\u8d44\u6e90\u6cc4\u6f0f", "\u672a\u91ca\u653e", "memory leak", "resource leak"), "resource_leak"),
        (("\u7c7b\u578b\u8f6c\u6362", "\u9690\u5f0f\u8f6c\u6362", "\u5f3a\u5236\u8f6c\u6362", "type conversion", "implicit cast"), "integer_safety"),
        (("\u6574\u6570", "\u6ea2\u51fa", "\u4e0b\u6ea2", "\u9664\u96f6", "divide by zero"), "integer_safety"),
        (("\u8f93\u5165", "\u53c2\u6570", "\u6821\u9a8c", "\u9a8c\u8bc1", "\u65ad\u8a00", "argument", "parameter"), "input_validation"),
        (("\u5e76\u53d1", "\u7ebf\u7a0b", "\u7ade\u6001", "\u6b7b\u9501", "concurrency", "race"), "concurrency"),
        (("\u517c\u5bb9", "\u79fb\u690d", "\u5e73\u53f0", "\u7c7b\u578b\u5b89\u5168", "\u7c7b\u578b\u4e0d\u5339\u914d", "compatibility", "type safety", "type mismatch"), "compatibility"),
        (("\u5b89\u5168", "\u6f0f\u6d1e", "\u6ce8\u5165", "security"), "security"),
    )

    confidence_aliases = {
        "high": 0.9,
        "medium": 0.7,
        "low": 0.5,
        "sure": 0.95,
        "certain": 0.95,
        "likely": 0.75,
        "possible": 0.5,
        "uncertain": 0.3,
        "\u9ad8": 0.9,
        "\u4e2d": 0.7,
        "\u4f4e": 0.5,
    }

    def sanitize(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"summary": "\u6a21\u578b\u8fd4\u56de\u4e3a\u7a7a\u6216\u683c\u5f0f\u5f02\u5e38\uff0c\u5df2\u964d\u7ea7\u5904\u7406\u3002", "score": 80.0, "findings": []}

        findings = value.get("findings")
        if not isinstance(findings, list):
            findings = []

        return {
            "summary": _truncate(_as_text(value.get("summary"), default="\u5ba1\u67e5\u5b8c\u6210\u3002"), 240, default="\u5ba1\u67e5\u5b8c\u6210\u3002"),
            "score": _as_score(value.get("score")),
            "findings": [self._sanitize_finding(item) for item in findings[:MAX_FINDINGS] if isinstance(item, dict)],
        }

    def _sanitize_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        fallback_line = _as_line(finding.get("line"))
        title = _truncate(_as_text(finding.get("title"), default="\u6a21\u578b\u53d1\u73b0\u7684\u95ee\u9898"), 120, default="\u6a21\u578b\u53d1\u73b0\u7684\u95ee\u9898")
        description = _truncate(_as_text(finding.get("description"), default=title), 360, default=title)
        remediation = _truncate(
            _as_text(finding.get("remediation"), default="\u8bf7\u7ed3\u5408\u4e0a\u4e0b\u6587\u68c0\u67e5\u5e76\u4fee\u590d\u8be5\u95ee\u9898\u3002"),
            360,
            default="\u8bf7\u7ed3\u5408\u4e0a\u4e0b\u6587\u68c0\u67e5\u5e76\u4fee\u590d\u8be5\u95ee\u9898\u3002",
        )
        return {
            "severity": self._normalize_severity(finding.get("severity")),
            "category": self._normalize_category(
                finding.get("category"),
                fallback_text=" ".join((title, description, remediation)),
            ),
            "title": title,
            "description": description,
            "file_path": _truncate(_as_text(finding.get("file_path"), default="unknown.c"), 512, default="unknown.c"),
            "line": fallback_line,
            "evidence_ids": self._normalize_string_list(finding.get("evidence_ids"), prefix="E", limit=12),
            "call_chain": self._normalize_string_list(finding.get("call_chain"), prefix=None, limit=16),
            "confidence": self._normalize_confidence(finding.get("confidence")),
            "remediation": remediation,
            "code_snippet": self._normalize_snippet(finding.get("code_snippet"), fallback_line=fallback_line),
            "fixed_snippet": self._normalize_snippet(finding.get("fixed_snippet"), fallback_line=fallback_line),
        }

    def _normalize_severity(self, value: Any) -> str:
        if isinstance(value, FindingSeverity):
            return value.value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in self.severity_aliases:
                return self.severity_aliases[normalized]
            stripped = value.strip()
            if stripped in self.severity_aliases:
                return self.severity_aliases[stripped]
        return "low"

    def _normalize_category(self, value: Any, *, fallback_text: str = "") -> str:
        if isinstance(value, FindingCategory):
            return value.value
        text = _as_text(value)
        lowered = text.lower().replace("-", "_")
        if lowered in self.category_aliases:
            return self.category_aliases[lowered]
        spaced = text.lower().replace("_", " ").replace("-", " ")
        if spaced in self.category_aliases:
            return self.category_aliases[spaced]
        return self._category_from_text(text) or self._category_from_text(fallback_text) or "logic"

    def _category_from_text(self, value: str) -> str | None:
        normalized = value.strip().lower()
        if not normalized:
            return None
        for tokens, category in self.category_keywords:
            if any(token in normalized for token in tokens):
                return category
        return None

    def _normalize_confidence(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            confidence = float(value)
        elif isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in self.confidence_aliases:
                return self.confidence_aliases[normalized]
            try:
                confidence = float(normalized.rstrip("%"))
            except ValueError:
                return None
        else:
            return None
        if confidence > 1:
            confidence = confidence / 100
        return max(0.0, min(1.0, confidence))

    def _normalize_string_list(self, value: Any, *, prefix: str | None, limit: int) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = _as_text(item)
            if not text:
                continue
            if prefix is not None and not text.startswith(prefix):
                continue
            result.append(text)
            if len(result) >= limit:
                break
        return result

    def _normalize_snippet(self, value: Any, *, fallback_line: int | None) -> list[dict[str, Any]]:
        if isinstance(value, (str, dict)):
            value = [value]
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            kind = "context"
            if isinstance(item, str):
                line = fallback_line
                content = item
            elif isinstance(item, dict):
                line = _as_line(item.get("line")) or fallback_line
                content = _as_text(item.get("content"))
                item_kind = _as_text(item.get("kind")).lower()
                if item_kind in {"context", "removed", "added"}:
                    kind = item_kind
            else:
                continue
            if line is None or not content:
                continue
            result.append({"line": line, "content": content[:1000], "kind": kind})
            if len(result) >= MAX_MODEL_SNIPPET_LINES:
                break
        return result


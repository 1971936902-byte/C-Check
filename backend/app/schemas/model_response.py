from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FindingSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SUGGESTION = "suggestion"


class FindingCategory(str, Enum):
    MEMORY_SAFETY = "memory_safety"
    BUFFER_OVERFLOW = "buffer_overflow"
    POINTER_SAFETY = "pointer_safety"
    RESOURCE_LEAK = "resource_leak"
    LOGIC = "logic"
    SECURITY = "security"
    INPUT_VALIDATION = "input_validation"
    INTEGER_SAFETY = "integer_safety"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    STYLE = "style"
    MAINTAINABILITY = "maintainability"
    COMPATIBILITY = "compatibility"
    PORTABILITY = "portability"


class CodeLineKind(str, Enum):
    CONTEXT = "context"
    REMOVED = "removed"
    ADDED = "added"


class CodeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: int = Field(ge=1)
    content: str
    kind: CodeLineKind = CodeLineKind.CONTEXT


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: FindingSeverity
    category: FindingCategory
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=360)
    file_path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)
    remediation: str = Field(min_length=1, max_length=360)
    code_snippet: list[CodeLine] = Field(default_factory=list, max_length=2)
    fixed_snippet: list[CodeLine] = Field(default_factory=list, max_length=2)


class ModelReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=240)
    score: float = Field(ge=0, le=100)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=5)

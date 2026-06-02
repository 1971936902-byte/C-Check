from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FindingSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SUGGESTION = "suggestion"


class FindingCategory(str, Enum):
    MEMORY_SAFETY = "memory_safety"
    LOGIC = "logic"
    SECURITY = "security"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    STYLE = "style"
    PORTABILITY = "portability"


class CodeLineKind(str, Enum):
    CONTEXT = "context"
    REMOVED = "removed"
    ADDED = "added"


class CodeLine(BaseModel):
    line: int = Field(ge=1)
    content: str
    kind: CodeLineKind = CodeLineKind.CONTEXT


class ReviewFinding(BaseModel):
    severity: FindingSeverity
    category: FindingCategory
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    file_path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)
    remediation: str = Field(min_length=1)
    code_snippet: list[CodeLine] = Field(default_factory=list)
    fixed_snippet: list[CodeLine] = Field(default_factory=list)


class ModelReviewResponse(BaseModel):
    summary: str = Field(min_length=1)
    score: float = Field(ge=0, le=100)
    findings: list[ReviewFinding] = Field(default_factory=list)

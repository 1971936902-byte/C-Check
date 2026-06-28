from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

COMPACT_MAX_FINDINGS = 200


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
    OTHER = "other"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: FindingSeverity
    category: FindingCategory
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=360)
    file_path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)


class ModelReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=240)
    score: float = Field(ge=0, le=100)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=2000)


FastFindingCategory = Literal[
    "buffer_overflow",
    "pointer_safety",
    "memory_safety",
    "resource_leak",
    "integer_safety",
    "input_validation",
    "concurrency",
    "logic",
    "other",
]


class CompactReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: FindingSeverity
    category: FastFindingCategory
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=180)
    file_path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)


class CompactModelReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0, le=100)
    findings: list[CompactReviewFinding] = Field(default_factory=list, max_length=COMPACT_MAX_FINDINGS)


class CandidateConfirmationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(ge=1)
    action: Literal["confirm", "reject", "correct"]
    category: str | None = Field(default=None, max_length=120)
    raw_category: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=360)
    line: int | None = Field(default=None, ge=1)
    trigger_kind: str | None = Field(default=None, max_length=80)
    reason: str = Field(default="", max_length=180)


class CandidateConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0, le=100)
    decisions: list[CandidateConfirmationDecision] = Field(default_factory=list, max_length=COMPACT_MAX_FINDINGS)

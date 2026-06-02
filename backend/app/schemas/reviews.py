from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import TaskStatus


class TextReviewRequest(BaseModel):
    model_node_id: str = Field(min_length=1, max_length=36)
    source_text: str
    check_types: list[str] = Field(min_length=1)


class ReviewFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    relative_path: str
    size_bytes: int


class ReviewTaskSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    tester_name: str
    model_node_id: str
    input_mode: str
    display_name: str
    status: TaskStatus
    progress: int
    error_message: str | None
    duration_ms: int | None
    file_count: int
    finding_count: int
    check_types: list[str]
    report_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewTaskResponse(ReviewTaskSummaryResponse):
    files: list[ReviewFileResponse] = Field(default_factory=list)

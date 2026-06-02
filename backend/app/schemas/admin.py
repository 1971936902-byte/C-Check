from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import TaskStatus


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=1024)
    role: str = Field(default="user", pattern="^(user|admin)$")


class UserEnabledRequest(BaseModel):
    is_enabled: bool


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    is_enabled: bool
    created_at: datetime


class ModelNodeRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    model_identifier: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    is_enabled: bool = True
    description: str | None = None


class ModelEnabledRequest(BaseModel):
    is_enabled: bool


class PromptUpdateRequest(BaseModel):
    body: str = Field(min_length=1)


class AdminModelNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    model_identifier: str
    base_url: str
    timeout_seconds: int
    is_enabled: bool
    is_default: bool
    description: str | None
    created_at: datetime


class PromptCreateRequest(BaseModel):
    body: str = Field(min_length=1)


class PromptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int
    body: str
    is_active: bool
    creator_id: str | None
    created_at: datetime


class AdminTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    model_node_id: str
    display_name: str
    status: TaskStatus
    progress: int
    finding_count: int
    error_message: str | None
    created_at: datetime


class DashboardResponse(BaseModel):
    users: int
    enabled_users: int
    models: int
    enabled_models: int
    tasks: int
    queued_tasks: int
    running_tasks: int
    completed_tasks: int
    failed_tasks: int

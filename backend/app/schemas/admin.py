from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ModelDeploymentStatus, TaskStatus


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


class ModelCatalogItemResponse(BaseModel):
    key: str
    display_name: str
    model_identifier: str
    description: str | None = None
    recommended_source: str = "huggingface"
    huggingface_repo: str | None = None
    modelscope_repo: str | None = None
    default_port: int | None = None
    default_served_model_name: str | None = None
    estimated_vram_gb: int | None = None
    tags: list[str] = Field(default_factory=list)


class ModelDeploymentCreateRequest(BaseModel):
    catalog_key: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    model_identifier: str | None = Field(default=None, max_length=255)
    source: str = Field(default="huggingface", pattern="^(huggingface|modelscope|local)$")
    source_repository: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    served_model_name: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    model_dir: str | None = Field(default=None, max_length=512)
    service_name: str | None = Field(default=None, max_length=128)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    auto_register: bool = True


class ModelDeploymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    catalog_key: str | None
    display_name: str
    model_identifier: str
    source: str
    source_repository: str
    served_model_name: str
    base_url: str
    port: int | None
    model_dir: str | None
    service_name: str | None
    status: ModelDeploymentStatus
    progress: int
    log: str | None
    error_message: str | None
    model_node_id: str | None
    created_at: datetime
    updated_at: datetime


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
    model_log: str | None
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


class SystemResourceResponse(BaseModel):
    cpu_percent: float | None = None
    load_average_1m: float | None = None
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    memory_percent: float | None = None
    disk_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_percent: float | None = None


class GpuDeviceResponse(BaseModel):
    index: int
    name: str
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    memory_percent: float | None = None
    temperature_c: float | None = None
    power_w: float | None = None


class ModelRuntimeMetricResponse(BaseModel):
    node_id: str
    display_name: str
    base_url: str
    metrics_available: bool
    prompt_throughput_tps: float | None = None
    generation_throughput_tps: float | None = None
    running_requests: int | None = None
    pending_requests: int | None = None
    gpu_kv_cache_usage_percent: float | None = None
    error: str | None = None


class ResourceSnapshotResponse(BaseModel):
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    system: SystemResourceResponse
    gpus: list[GpuDeviceResponse] = Field(default_factory=list)
    models: list[ModelRuntimeMetricResponse] = Field(default_factory=list)
    tasks: DashboardResponse

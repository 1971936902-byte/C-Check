from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelDeploymentStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    review_tasks: Mapped[list[ReviewTask]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )
    prompt_versions: Mapped[list[PromptVersion]] = relationship(back_populates="creator")


class ModelNode(TimestampMixin, Base):
    __tablename__ = "model_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(512))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    gpu_indices: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    tensor_parallel_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    review_tasks: Mapped[list[ReviewTask]] = relationship(back_populates="model_node")
    deployments: Mapped[list[ModelDeployment]] = relationship(back_populates="model_node")


class ModelDeployment(TimestampMixin, Base):
    __tablename__ = "model_deployments"
    __table_args__ = (Index("ix_model_deployments_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    catalog_key: Mapped[str | None] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    served_model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    model_dir: Mapped[str | None] = mapped_column(String(512))
    service_name: Mapped[str | None] = mapped_column(String(128))
    gpu_indices: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    tensor_parallel_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[ModelDeploymentStatus] = mapped_column(
        Enum(
            ModelDeploymentStatus,
            native_enum=False,
            values_callable=lambda statuses: [s.value for s in statuses],
        ),
        default=ModelDeploymentStatus.QUEUED,
        nullable=False,
        index=True,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    log: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    model_node_id: Mapped[str | None] = mapped_column(ForeignKey("model_nodes.id", ondelete="SET NULL"))

    model_node: Mapped[ModelNode | None] = relationship(back_populates="deployments")


class PromptVersion(TimestampMixin, Base):
    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    creator_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    creator: Mapped[User | None] = relationship(back_populates="prompt_versions")


class ReviewTask(TimestampMixin, Base):
    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_tasks_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_node_id: Mapped[str] = mapped_column(ForeignKey("model_nodes.id"), nullable=False, index=True)
    input_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, values_callable=lambda statuses: [s.value for s in statuses]),
        default=TaskStatus.QUEUED,
        nullable=False,
        index=True,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queue_priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    model_log: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    check_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    owner: Mapped[User] = relationship(back_populates="review_tasks")
    model_node: Mapped[ModelNode] = relationship(back_populates="review_tasks")
    files: Mapped[list[ReviewFile]] = relationship(back_populates="task", cascade="all, delete-orphan")
    report: Mapped[Report | None] = relationship(back_populates="task", cascade="all, delete-orphan")
    code_project: Mapped[CodeProject | None] = relationship(back_populates="task", cascade="all, delete-orphan")
    review_contexts: Mapped[list[ReviewContext]] = relationship(back_populates="task", cascade="all, delete-orphan")

    @property
    def report_id(self) -> str | None:
        return self.report.id if self.report is not None else None

    @property
    def tester_name(self) -> str:
        return self.owner.username

    @property
    def queued_ahead_count(self) -> int | None:
        return getattr(self, "_queued_ahead_count", None)

    @queued_ahead_count.setter
    def queued_ahead_count(self, value: int | None) -> None:
        self._queued_ahead_count = value


class ReviewFile(TimestampMixin, Base):
    __tablename__ = "review_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    source_text: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    task: Mapped[ReviewTask] = relationship(back_populates="files")


class Report(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    high_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    medium_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suggestion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    category_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    task: Mapped[ReviewTask] = relationship(back_populates="report")


class CodeProject(TimestampMixin, Base):
    __tablename__ = "code_projects"
    __table_args__ = (
        Index("ix_code_projects_task_id", "task_id"),
        Index("ix_code_projects_source_hash", "source_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), unique=True, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_backend: Mapped[str | None] = mapped_column(String(64))
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    task: Mapped[ReviewTask] = relationship(back_populates="code_project")
    files: Mapped[list[CodeFile]] = relationship(back_populates="project", cascade="all, delete-orphan")
    symbols: Mapped[list[CodeSymbol]] = relationship(back_populates="project", cascade="all, delete-orphan")
    edges: Mapped[list[CodeEdge]] = relationship(back_populates="project", cascade="all, delete-orphan")
    chunks: Mapped[list[CodeChunk]] = relationship(back_populates="project", cascade="all, delete-orphan")
    embeddings: Mapped[list[CodeEmbedding]] = relationship(back_populates="project", cascade="all, delete-orphan")


class CodeFile(TimestampMixin, Base):
    __tablename__ = "code_files"
    __table_args__ = (
        Index("ix_code_files_project_path", "project_id", "relative_path", unique=True),
        Index("ix_code_files_content_hash", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("code_projects.id", ondelete="CASCADE"), nullable=False)
    review_file_id: Mapped[str | None] = mapped_column(ForeignKey("review_files.id", ondelete="SET NULL"))
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(32), default="c", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    project: Mapped[CodeProject] = relationship(back_populates="files")
    symbols: Mapped[list[CodeSymbol]] = relationship(back_populates="file", cascade="all, delete-orphan")
    chunks: Mapped[list[CodeChunk]] = relationship(back_populates="file", cascade="all, delete-orphan")


class CodeSymbol(TimestampMixin, Base):
    __tablename__ = "code_symbols"
    __table_args__ = (
        Index("ix_code_symbols_project_name", "project_id", "name"),
        Index("ix_code_symbols_project_kind", "project_id", "kind"),
        Index("ix_code_symbols_file_range", "file_id", "start_line", "end_line"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("code_projects.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[str] = mapped_column(ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(32), default="global", nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)
    source_tool: Mapped[str] = mapped_column(String(64), nullable=False)

    project: Mapped[CodeProject] = relationship(back_populates="symbols")
    file: Mapped[CodeFile] = relationship(back_populates="symbols")
    chunks: Mapped[list[CodeChunk]] = relationship(back_populates="symbol")


class CodeEdge(TimestampMixin, Base):
    __tablename__ = "code_edges"
    __table_args__ = (
        Index("ix_code_edges_project_type", "project_id", "edge_type"),
        Index("ix_code_edges_source", "source_id"),
        Index("ix_code_edges_target", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("code_projects.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(36))
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False)
    line: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)
    source_tool: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[CodeProject] = relationship(back_populates="edges")


class CodeChunk(TimestampMixin, Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        Index("ix_code_chunks_project_symbol", "project_id", "symbol_id"),
        Index("ix_code_chunks_project_kind", "project_id", "chunk_kind"),
        Index("ix_code_chunks_content_hash", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("code_projects.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[str] = mapped_column(ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False)
    symbol_id: Mapped[str | None] = mapped_column(ForeignKey("code_symbols.id", ondelete="SET NULL"))
    chunk_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol_name: Mapped[str | None] = mapped_column(String(255))
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[CodeProject] = relationship(back_populates="chunks")
    file: Mapped[CodeFile] = relationship(back_populates="chunks")
    symbol: Mapped[CodeSymbol | None] = relationship(back_populates="chunks")


class CodeEmbedding(TimestampMixin, Base):
    __tablename__ = "code_embeddings"
    __table_args__ = (
        Index("ix_code_embeddings_project_model", "project_id", "embedding_model"),
        Index("ix_code_embeddings_chunk_model", "chunk_id", "embedding_model"),
        Index("ix_code_embeddings_vector_id", "vector_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("code_projects.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("code_chunks.id", ondelete="CASCADE"), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[CodeProject] = relationship(back_populates="embeddings")
    chunk: Mapped[CodeChunk] = relationship()


class ReviewContext(TimestampMixin, Base):
    __tablename__ = "review_contexts"
    __table_args__ = (
        Index("ix_review_contexts_task_id", "task_id"),
        Index("ix_review_contexts_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("code_projects.id", ondelete="SET NULL"))
    context_text: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    task: Mapped[ReviewTask] = relationship(back_populates="review_contexts")
    evidence_items: Mapped[list[ReviewEvidence]] = relationship(
        back_populates="context", cascade="all, delete-orphan"
    )


class ReviewEvidence(TimestampMixin, Base):
    __tablename__ = "review_evidence"
    __table_args__ = (
        Index("ix_review_evidence_context_id", "context_id"),
        Index("ix_review_evidence_task_key", "task_id", "evidence_key"),
        Index("ix_review_evidence_chunk_id", "chunk_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False)
    context_id: Mapped[str] = mapped_column(ForeignKey("review_contexts.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[str | None] = mapped_column(ForeignKey("code_chunks.id", ondelete="SET NULL"))
    evidence_key: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    symbol_name: Mapped[str | None] = mapped_column(String(255))
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    context: Mapped[ReviewContext] = relationship(back_populates="evidence_items")

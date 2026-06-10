from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ModelDeployment, ModelDeploymentStatus, ModelNode, User
from app.db.session import SessionLocal
from app.schemas.admin import ModelDeploymentCreateRequest


class ModelCatalogItem(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    model_identifier: str = Field(min_length=1, max_length=255)
    description: str | None = None
    recommended_source: str = "huggingface"
    huggingface_repo: str | None = None
    modelscope_repo: str | None = None
    default_port: int | None = None
    default_served_model_name: str | None = None
    estimated_vram_gb: int | None = None
    tags: list[str] = Field(default_factory=list)


def list_model_catalog(settings: Settings | None = None) -> list[ModelCatalogItem]:
    settings = settings or get_settings()
    path = settings.model_catalog_path
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ModelCatalogItem.model_validate(item) for item in data]


def _catalog_by_key(settings: Settings, key: str | None) -> ModelCatalogItem | None:
    if not key:
        return None
    return next((item for item in list_model_catalog(settings) if item.key == key), None)


def _source_repository(catalog: ModelCatalogItem | None, request: ModelDeploymentCreateRequest) -> str:
    if request.source_repository:
        return request.source_repository
    if catalog is None:
        raise ValueError("source_repository is required for custom model deployments")
    if request.source == "modelscope" and catalog.modelscope_repo:
        return catalog.modelscope_repo
    if request.source == "huggingface" and catalog.huggingface_repo:
        return catalog.huggingface_repo
    if request.source == "local":
        return request.model_dir or catalog.model_identifier
    return catalog.model_identifier


def _deployment_port(catalog: ModelCatalogItem | None, request: ModelDeploymentCreateRequest) -> int | None:
    return request.port or (catalog.default_port if catalog else None)


def _deployment_base_url(catalog: ModelCatalogItem | None, request: ModelDeploymentCreateRequest) -> str:
    if request.base_url:
        return request.base_url
    port = _deployment_port(catalog, request)
    if port:
        return f"http://127.0.0.1:{port}"
    raise ValueError("base_url or port is required")


def _append_log(existing: str | None, line: str) -> str:
    return f"{existing.rstrip()}\n{line}" if existing else line


def create_model_deployment(
    db: Session,
    request: ModelDeploymentCreateRequest,
    admin: User,
    settings: Settings | None = None,
) -> ModelDeployment:
    settings = settings or get_settings()
    catalog = _catalog_by_key(settings, request.catalog_key)
    if request.catalog_key and catalog is None:
        raise ValueError("catalog model not found")

    display_name = request.display_name or (catalog.display_name if catalog else None)
    model_identifier = request.model_identifier or (catalog.model_identifier if catalog else None)
    if not display_name or not model_identifier:
        raise ValueError("display_name and model_identifier are required")

    served_model_name = request.served_model_name or (
        catalog.default_served_model_name if catalog else model_identifier.split("/")[-1]
    )
    source_repository = _source_repository(catalog, request)
    port = _deployment_port(catalog, request)
    base_url = _deployment_base_url(catalog, request)
    api_key = request.api_key or settings.vllm_api_key
    node: ModelNode | None = None
    if request.auto_register:
        node = ModelNode(
            display_name=display_name,
            model_identifier=served_model_name,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=request.timeout_seconds,
            is_enabled=False,
            is_default=False,
            description=f"由模型部署任务自动登记：{source_repository}",
        )
        db.add(node)
        db.flush()

    deployment = ModelDeployment(
        catalog_key=request.catalog_key,
        display_name=display_name,
        model_identifier=model_identifier,
        source=request.source,
        source_repository=source_repository,
        served_model_name=served_model_name,
        base_url=base_url,
        port=port,
        model_dir=request.model_dir,
        service_name=request.service_name or f"c-check-vllm-{served_model_name}".replace("/", "-"),
        status=ModelDeploymentStatus.QUEUED,
        progress=0,
        created_by_id=admin.id,
        model_node_id=node.id if node else None,
        log="Deployment task queued.",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def deployment_command(deployment: ModelDeployment, settings: Settings) -> list[str]:
    script = settings.model_deployment_script
    return [
        "bash",
        str(script),
        "--source",
        deployment.source,
        "--repository",
        deployment.source_repository,
        "--served-model-name",
        deployment.served_model_name,
        "--base-url",
        deployment.base_url,
        "--port",
        str(deployment.port or ""),
        "--service-name",
        deployment.service_name or "",
        "--model-dir",
        deployment.model_dir or "",
    ]


def _manual_instruction(deployment: ModelDeployment, settings: Settings) -> str:
    command = " ".join(shlex.quote(part) for part in deployment_command(deployment, settings) if part)
    return (
        "MODEL_DEPLOYMENT_ENABLED is false; automatic download/deploy was not executed. "
        f"Enable it on the Linux GPU server or run manually: {command}"
    )


def run_model_deployment(deployment_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with SessionLocal() as db:
        deployment = db.get(ModelDeployment, deployment_id)
        if deployment is None:
            return
        if not settings.model_deployment_enabled:
            deployment.status = ModelDeploymentStatus.MANUAL_REQUIRED
            deployment.progress = 10
            deployment.log = _append_log(deployment.log, _manual_instruction(deployment, settings))
            db.commit()
            return
        if not Path(settings.model_deployment_script).exists():
            deployment.status = ModelDeploymentStatus.FAILED
            deployment.progress = 100
            deployment.error_message = "model deployment script not found"
            deployment.log = _append_log(deployment.log, deployment.error_message)
            db.commit()
            return

        deployment.status = ModelDeploymentStatus.RUNNING
        deployment.progress = 20
        deployment.error_message = None
        deployment.log = _append_log(deployment.log, "Starting model deployment script.")
        db.commit()

    env = os.environ.copy()
    if settings.vllm_api_key:
        env["VLLM_API_KEY"] = settings.vllm_api_key

    process = subprocess.run(
        deployment_command(deployment, settings),
        text=True,
        capture_output=True,
        check=False,
        timeout=24 * 60 * 60,
        env=env,
    )

    with SessionLocal() as db:
        deployment = db.get(ModelDeployment, deployment_id)
        if deployment is None:
            return
        output = "\n".join(part for part in [process.stdout, process.stderr] if part)
        deployment.log = _append_log(deployment.log, output[-16000:] or "Deployment script finished.")
        deployment.progress = 100
        if process.returncode == 0:
            deployment.status = ModelDeploymentStatus.SUCCEEDED
            deployment.error_message = None
            if deployment.model_node is not None:
                for node in db.scalars(select(ModelNode).where(ModelNode.id != deployment.model_node.id)).all():
                    node.is_enabled = False
                    node.is_default = False
                if not deployment.model_node.api_key and settings.vllm_api_key:
                    deployment.model_node.api_key = settings.vllm_api_key
                deployment.model_node.is_enabled = True
                deployment.model_node.is_default = True
        else:
            deployment.status = ModelDeploymentStatus.FAILED
            deployment.error_message = f"deployment script exited with {process.returncode}"
        db.commit()


def start_model_deployment(deployment_id: str) -> None:
    thread = threading.Thread(target=run_model_deployment, args=(deployment_id,), daemon=True)
    thread.start()

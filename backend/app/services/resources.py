from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ModelNode, ReviewTask, TaskStatus, User
from app.schemas.admin import (
    DashboardResponse,
    GpuDeviceResponse,
    ModelRuntimeMetricResponse,
    ResourceSnapshotResponse,
    SystemResourceResponse,
)


def _read_cpu_times() -> tuple[int, int] | None:
    stat_path = Path("/proc/stat")
    if not stat_path.exists():
        return None
    first = stat_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()
    if not first or first[0] != "cpu":
        return None
    values = [int(value) for value in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _cpu_percent() -> float | None:
    first = _read_cpu_times()
    if first is None:
        return None
    time.sleep(0.08)
    second = _read_cpu_times()
    if second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)


def _memory() -> tuple[int | None, int | None, float | None]:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None, None, None
    values: dict[str, int] = {}
    for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        key, _, value = line.partition(":")
        parts = value.strip().split()
        if parts:
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return total, None, None
    used = total - available
    return total, used, round(used / total * 100, 2)


def _system_resources(settings: Settings) -> SystemResourceResponse:
    memory_total, memory_used, memory_percent = _memory()
    disk_target = settings.storage_path if settings.storage_path.exists() else Path("/")
    disk = shutil.disk_usage(disk_target)
    try:
        load_average_1m = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load_average_1m = None
    return SystemResourceResponse(
        cpu_percent=_cpu_percent(),
        load_average_1m=load_average_1m,
        memory_total_bytes=memory_total,
        memory_used_bytes=memory_used,
        memory_percent=memory_percent,
        disk_total_bytes=disk.total,
        disk_used_bytes=disk.used,
        disk_percent=round(disk.used / disk.total * 100, 2) if disk.total else None,
    )


def _float_or_none(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


def _gpu_devices() -> list[GpuDeviceResponse]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    devices: list[GpuDeviceResponse] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        index_value = _float_or_none(parts[0])
        memory_used = _float_or_none(parts[3])
        memory_total = _float_or_none(parts[4])
        memory_percent = (
            round(memory_used / memory_total * 100, 2)
            if memory_used is not None and memory_total
            else None
        )
        devices.append(
            GpuDeviceResponse(
                index=int(index_value or len(devices)),
                name=parts[1],
                utilization_percent=_float_or_none(parts[2]),
                memory_used_mb=memory_used,
                memory_total_mb=memory_total,
                memory_percent=memory_percent,
                temperature_c=_float_or_none(parts[5]),
                power_w=_float_or_none(parts[6]),
            )
        )
    return devices


def _metric_value(metrics: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        pattern = re.compile(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([-+]?\d+(?:\.\d+)?)$", re.MULTILINE)
        match = pattern.search(metrics)
        if match:
            return float(match.group(1))
    return None


def _model_metrics(node: ModelNode) -> ModelRuntimeMetricResponse:
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else None
    try:
        with httpx.Client(timeout=3) as client:
            response = client.get(f"{node.base_url.rstrip('/')}/metrics", headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return ModelRuntimeMetricResponse(
            node_id=node.id,
            display_name=node.display_name,
            base_url=node.base_url,
            metrics_available=False,
            error=str(exc)[:300],
        )
    metrics = response.text
    running = _metric_value(metrics, ("vllm:num_requests_running", "vllm_num_requests_running"))
    pending = _metric_value(metrics, ("vllm:num_requests_waiting", "vllm_num_requests_waiting"))
    return ModelRuntimeMetricResponse(
        node_id=node.id,
        display_name=node.display_name,
        base_url=node.base_url,
        metrics_available=True,
        prompt_throughput_tps=_metric_value(
            metrics,
            ("vllm:avg_prompt_throughput_toks_per_s", "vllm_avg_prompt_throughput_toks_per_s"),
        ),
        generation_throughput_tps=_metric_value(
            metrics,
            ("vllm:avg_generation_throughput_toks_per_s", "vllm_avg_generation_throughput_toks_per_s"),
        ),
        running_requests=int(running) if running is not None else None,
        pending_requests=int(pending) if pending is not None else None,
        gpu_kv_cache_usage_percent=_metric_value(
            metrics,
            ("vllm:gpu_cache_usage_perc", "vllm_gpu_cache_usage_perc"),
        ),
    )


def _dashboard(db: Session) -> DashboardResponse:
    statuses = dict(db.execute(select(ReviewTask.status, func.count()).group_by(ReviewTask.status)).all())
    return DashboardResponse(
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        enabled_users=db.scalar(select(func.count()).select_from(User).where(User.is_enabled.is_(True))) or 0,
        models=db.scalar(select(func.count()).select_from(ModelNode)) or 0,
        enabled_models=db.scalar(select(func.count()).select_from(ModelNode).where(ModelNode.is_enabled.is_(True))) or 0,
        tasks=sum(statuses.values()),
        queued_tasks=statuses.get(TaskStatus.QUEUED, 0),
        running_tasks=statuses.get(TaskStatus.RUNNING, 0),
        completed_tasks=statuses.get(TaskStatus.COMPLETED, 0),
        failed_tasks=statuses.get(TaskStatus.FAILED, 0),
    )


def collect_resource_snapshot(db: Session, settings: Settings) -> ResourceSnapshotResponse:
    nodes = list(db.scalars(select(ModelNode).where(ModelNode.is_enabled.is_(True)).order_by(ModelNode.created_at.desc())).all())
    return ResourceSnapshotResponse(
        system=_system_resources(settings),
        gpus=_gpu_devices(),
        models=[_model_metrics(node) for node in nodes],
        tasks=_dashboard(db),
    )

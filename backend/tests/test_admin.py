from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.db.models import ModelNode, User
from app.schemas.admin import (
    DashboardResponse,
    GpuDeviceResponse,
    ModelRuntimeMetricResponse,
    ResourceSnapshotResponse,
    SystemResourceResponse,
)


JWT_SECRET = "test-jwt-secret-at-least-32-characters-long"


@pytest.fixture(autouse=True)
def use_secure_test_jwt_secret(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def auth_headers(user_id: str) -> dict[str, str]:
    token = create_access_token(user_id, 0, JWT_SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def test_model_update_preserves_existing_api_key_when_field_is_omitted(db_session_factory):
    from app.main import app

    with db_session_factory() as db:
        admin = User(username="admin-user", password_hash=hash_password("admin-password"), role="admin")
        node = ModelNode(
            display_name="Qwen node",
            model_identifier="qwen-model",
            base_url="http://model-node",
            api_key="existing-secret",
            timeout_seconds=120,
            is_enabled=True,
            description="Original",
        )
        db.add_all([admin, node])
        db.commit()
        admin_id = admin.id
        node_id = node.id

    with TestClient(app) as client:
        response = client.put(
            f"/api/admin/models/{node_id}",
            headers=auth_headers(admin_id),
            json={
                "display_name": "Updated Qwen node",
                "model_identifier": "qwen-model",
                "base_url": "http://model-node",
                "timeout_seconds": 180,
                "is_enabled": True,
                "description": "Updated",
            },
        )

    assert response.status_code == 200
    with db_session_factory() as db:
        node = db.get(ModelNode, node_id)
        assert node.api_key == "existing-secret"


def test_admin_resources_endpoint_returns_runtime_snapshot(db_session_factory, monkeypatch):
    from app.main import app

    snapshot = ResourceSnapshotResponse(
        system=SystemResourceResponse(
            cpu_percent=31.5,
            load_average_1m=1.2,
            memory_total_bytes=16_000,
            memory_used_bytes=8_000,
            memory_percent=50.0,
            disk_total_bytes=100_000,
            disk_used_bytes=25_000,
            disk_percent=25.0,
        ),
        gpus=[
            GpuDeviceResponse(
                index=0,
                name="NVIDIA Test GPU",
                utilization_percent=68.0,
                memory_used_mb=18_000.0,
                memory_total_mb=24_000.0,
                memory_percent=75.0,
                temperature_c=62.0,
                power_w=230.0,
            )
        ],
        models=[
            ModelRuntimeMetricResponse(
                node_id="model-1",
                display_name="Qwen",
                base_url="http://127.0.0.1:8001",
                metrics_available=True,
                prompt_throughput_tps=1200.0,
                generation_throughput_tps=150.0,
                running_requests=3,
                pending_requests=1,
                gpu_kv_cache_usage_percent=27.4,
            )
        ],
        tasks=DashboardResponse(
            users=1,
            enabled_users=1,
            models=1,
            enabled_models=1,
            tasks=4,
            queued_tasks=1,
            running_tasks=2,
            completed_tasks=1,
            failed_tasks=0,
        ),
    )
    monkeypatch.setattr("app.api.admin.collect_resource_snapshot", lambda db, settings: snapshot)

    with db_session_factory() as db:
        admin = User(username="admin-user", password_hash=hash_password("admin-password"), role="admin")
        user = User(username="normal-user", password_hash=hash_password("user-password"), role="user")
        db.add_all([admin, user])
        db.commit()
        admin_id = admin.id
        user_id = user.id

    with TestClient(app) as client:
        admin_response = client.get("/api/admin/resources", headers=auth_headers(admin_id))
        user_response = client.get("/api/admin/resources", headers=auth_headers(user_id))

    assert admin_response.status_code == 200
    payload = admin_response.json()
    assert payload["system"]["cpu_percent"] == 31.5
    assert payload["gpus"][0]["memory_percent"] == 75.0
    assert payload["models"][0]["generation_throughput_tps"] == 150.0
    assert payload["tasks"]["running_tasks"] == 2
    assert user_response.status_code == 403

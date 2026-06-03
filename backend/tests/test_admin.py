from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.db.models import ModelNode, User


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

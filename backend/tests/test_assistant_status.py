from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.models import AssistantComputeGate, User


def test_status_requires_authentication():
    from app.main import app
    with TestClient(app) as client:
        assert client.get('/api/assistant/status').status_code == 401


def test_disabled_and_uninitialized_status_fail_closed(monkeypatch):
    from app.api.deps import get_current_user
    from app.main import app
    app.dependency_overrides[get_current_user] = lambda: User(username='reader', password_hash='unused')
    try:
        with TestClient(app) as client:
            result = client.get('/api/assistant/status').json()
            assert result['enabled'] is False
            assert result['available'] is False
            monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
            get_settings.cache_clear()
            result = client.get('/api/assistant/status').json()
            assert result['enabled'] is True
            assert result['available'] is False
            assert result['reason'] == 'scheduler_not_initialized'
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_status_does_not_expose_internal_lease(db_session, monkeypatch):
    from app.api.deps import get_current_user
    from app.main import app
    from datetime import datetime, timezone
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    get_settings.cache_clear()
    db_session.add(AssistantComputeGate(id=1, generation=5, owner_token='secret-token',
        operation='chat', cancel_requested=False, heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: User(username='reader', password_hash='unused')
    try:
        with TestClient(app) as client:
            response = client.get('/api/assistant/status')
            assert response.status_code == 200
            assert response.json()['reason'] == 'assistant_busy'
            assert response.json()['available'] is False
            assert 'secret-token' not in response.text
            assert 'generation' not in response.json()
    finally:
        app.dependency_overrides.pop(get_current_user, None)

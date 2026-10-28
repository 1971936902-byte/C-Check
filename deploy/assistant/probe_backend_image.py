"""Smoke test the built backend image against a disposable MySQL database."""
import os
from pathlib import Path
import secrets


def main():
    password = secrets.token_hex(24)
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    import time
    host = os.environ['PROBE_MYSQL_HOST']
    if not host.startswith('ccheck-assistant-probe-'):
        raise RuntimeError('refusing a non-probe database host')
    url = URL.create('mysql+pymysql', username='probe',
        password=Path('/run/secrets/probe-mysql').read_text().strip(), host=host,
        database='ccheck_assistant_probe')
    readiness = create_engine(url, connect_args={'connect_timeout': 3})
    for attempt in range(60):
        try:
            with readiness.connect() as connection:
                connection.execute(text('SELECT 1'))
            break
        except Exception:
            if attempt == 59:
                raise RuntimeError('isolated MySQL did not become ready') from None
            time.sleep(2)
    readiness.dispose()
    os.environ.update(DATABASE_URL=url.render_as_string(hide_password=False),
        JWT_SECRET=secrets.token_hex(32), ADMIN_PASSWORD=password, ADMIN_USERNAME='probe-admin',
        STORAGE_PATH='/tmp/assistant-storage', ASSISTANT_ENABLED='true')
    from fastapi.testclient import TestClient
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.db.base import Base
    from app.db import session
    from app.db.models import AssistantComputeGate, ModelNode, ReviewTask, TaskStatus, User
    import app.services.assistant_embedding as embedding
    assert str(Path(embedding.__file__)).startswith('/opt/c-check-assistant/backend/')
    from app.main import app
    Base.metadata.create_all(session.engine)
    with session.SessionLocal() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    with TestClient(app) as client:
        assert client.get('/api/assistant/status').status_code == 401
        login = client.post('/api/auth/login', json={'username': 'probe-admin', 'password': password})
        assert login.status_code == 200
        headers = {'Authorization': 'Bearer ' + login.json()['access_token']}
        status = client.get('/api/assistant/status', headers=headers)
        assert status.status_code == 200 and status.json()['available'] is True
        assert client.get('/api/assistant/conversations', headers=headers).json() == []
        assert client.get('/api/assistant/documents', headers=headers).json() == []
        with session.SessionLocal() as db:
            db.add(ReviewTask(owner=User(username='review-owner', password_hash='unused'),
                model_node=ModelNode(display_name='Qwen', model_identifier='qwen2.5', base_url='http://unused'),
                input_mode='text', display_name='probe.c', status=TaskStatus.QUEUED, file_count=1))
            db.commit()
        result = client.get('/api/assistant/status', headers=headers).json()
        assert result['available'] is False and result['reason'] == 'review_busy'
        assert 'owner_token' not in result and 'control_token' not in result
    assert os.getuid() == 10001
    print('BACKEND_IMAGE_NONROOT_READONLY_SMOKE_OK: login, authorization, idle and review-busy states')


if __name__ == '__main__':
    main()

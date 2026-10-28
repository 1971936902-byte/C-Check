from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.core.config import get_settings
from app.db.models import AssistantComputeGate, AssistantDocument, User
from app.services.assistant_documents import document_path


@pytest.fixture
def setup(db_session, db_session_factory, monkeypatch, tmp_path):
    monkeypatch.setenv('STORAGE_PATH', str(tmp_path))
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    monkeypatch.setenv('ASSISTANT_ANYTHINGLLM_URL', 'http://anythingllm')
    monkeypatch.setenv('ASSISTANT_ANYTHINGLLM_API_KEY', 'test')
    get_settings.cache_clear()
    from app.main import app
    from app.api.deps import get_current_user
    from app.api.assistant import assistant_client
    user = User(username='document-owner', password_hash='unused')
    other = User(username='other-document-owner', password_hash='unused')
    db_session.add_all([user, other, AssistantComputeGate(id=1, generation=0, cancel_requested=False)])
    db_session.commit()
    active = [user]
    queued = Mock()
    provider = Mock()
    provider.upload_document.return_value = ['custom-documents/test.json']
    monkeypatch.setattr('app.api.assistant_documents.enqueue_document', queued)
    monkeypatch.setattr('app.tasks.assistant_documents.AnythingLLM', lambda *args, **kwargs: provider)
    monkeypatch.setattr('app.tasks.assistant_documents.SessionLocal', db_session_factory)
    app.dependency_overrides[get_current_user] = lambda: active[0]
    app.dependency_overrides[assistant_client] = lambda: provider
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, user=user, other=other, active=active,
                                  queued=queued, provider=provider, root=tmp_path)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(assistant_client, None)


def upload(setup):
    response = setup.client.post('/api/assistant/documents', files=[('files', ('main_c.txt', b'int main(){}'))])
    assert response.status_code == 202
    return response.json()[0]


def test_batch_upload_is_private_and_has_no_internal_paths(setup):
    response = setup.client.post('/api/assistant/documents', files=[
        ('files', ('../one.txt', b'first')), ('files', ('two.md', b'second'))])
    assert response.status_code == 202
    records = response.json()
    assert len(records) == 2
    assert records[0]['filename'] == 'one.txt'
    assert 'locations' not in records[0]
    assert 'owner_id' not in records[0]
    assert document_path(setup.user.id, records[0]['id']).read_bytes() == b'first'
    assert setup.queued.call_count == 2
    setup.active[0] = setup.other
    assert setup.client.get('/api/assistant/documents').json() == []
    assert setup.client.post('/api/assistant/documents/' + records[0]['id'] + '/retry').status_code == 404


def test_invalid_batch_removes_only_new_files_and_does_not_enqueue(setup):
    result = setup.client.post('/api/assistant/documents', files=[
        ('files', ('valid.txt', b'first')), ('files', ('empty.txt', b''))])
    assert result.status_code == 422
    assert list(setup.root.rglob('*.bin')) == []
    assert setup.client.get('/api/assistant/documents').json() == []
    setup.queued.assert_not_called()


def test_worker_success_and_duplicate_delivery(setup, db_session):
    from app.tasks.assistant_documents import run_document
    record = upload(setup)
    run_document(record['id'])
    run_document(record['id'])
    db_session.expire_all()
    document = db_session.get(AssistantDocument, record['id'])
    assert document.status == 'ready'
    assert document.locations == ['custom-documents/test.json']
    setup.provider.upload_document.assert_called_once()
    setup.provider.embed_documents.assert_called_once_with(setup.user.id, document.locations)


def test_busy_worker_pauses_without_upload_and_retry_reuses_file(setup, db_session):
    from app.tasks.assistant_documents import run_document
    from app.services import assistant_gate as gate
    record = upload(setup)
    token = gate.claim(db_session, 'chat')
    db_session.commit()
    run_document(record['id'])
    db_session.expire_all()
    assert db_session.get(AssistantDocument, record['id']).status == 'paused'
    setup.provider.upload_document.assert_not_called()
    assert setup.client.post('/api/assistant/documents/' + record['id'] + '/retry').status_code == 409
    gate.acknowledge_idle(db_session, token)
    db_session.commit()
    assert setup.client.post('/api/assistant/documents/' + record['id'] + '/retry').status_code == 202
    run_document(record['id'])
    db_session.expire_all()
    assert db_session.get(AssistantDocument, record['id']).status == 'ready'
    assert document_path(setup.user.id, record['id']).exists()


def test_queue_failure_keeps_uploaded_file_for_retry(setup):
    setup.queued.side_effect = RuntimeError('broker down')
    record = upload(setup)
    assert record['status'] == 'error'
    assert record['error'] == 'document_queue_unavailable'
    assert document_path(setup.user.id, record['id']).exists()


def test_upstream_timeout_is_not_immediately_retryable(setup, db_session):
    import httpx
    from app.services.anythingllm import AnythingLLMError
    from app.tasks.assistant_documents import run_document
    record = upload(setup)
    failure = AnythingLLMError('upstream unavailable')
    failure.__cause__ = httpx.ReadTimeout('unknown upstream outcome')
    setup.provider.embed_documents.side_effect = failure
    run_document(record['id'])
    db_session.expire_all()
    document = db_session.get(AssistantDocument, record['id'])
    assert document.status == 'recovery_required'
    assert document.locations == ['custom-documents/test.json']
    assert setup.client.post('/api/assistant/documents/' + record['id'] + '/retry').status_code == 409


def test_maintenance_pauses_worker_and_rejects_new_compute_but_keeps_list_readable(setup, db_session, monkeypatch):
    from app.tasks.assistant_documents import run_document
    record = upload(setup)
    marker = setup.root / 'maintenance'
    marker.touch()
    monkeypatch.setenv('DEPLOYMENT_MAINTENANCE_FILE', str(marker))
    get_settings.cache_clear()
    run_document(record['id'])
    db_session.expire_all()
    document = db_session.get(AssistantDocument, record['id'])
    assert document.status == 'paused' and document.error == 'maintenance'
    setup.provider.ensure_workspace.assert_not_called()
    assert setup.client.get('/api/assistant/documents').status_code == 200
    assert setup.client.post('/api/assistant/documents/' + record['id'] + '/retry').status_code == 503
    assert setup.client.post('/api/assistant/documents', files=[
        ('files', ('new.txt', b'content'))]).status_code == 503
    assert len(list(setup.root.rglob('*.bin'))) == 1
    assert setup.queued.call_count == 1
    marker.unlink()
    assert setup.client.post('/api/assistant/documents/' + record['id'] + '/retry').status_code == 202

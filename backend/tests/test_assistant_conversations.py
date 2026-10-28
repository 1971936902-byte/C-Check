from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.db.models import User, AssistantConversation
from app.services.anythingllm import AnythingLLMError


@pytest.fixture
def setup(db_session):
    from app.main import app
    from app.api.deps import get_current_user
    from app.api.assistant import assistant_client
    alice = User(username='alice-assistant', password_hash='unused')
    bob = User(username='bob-assistant', password_hash='unused', role='admin')
    db_session.add_all([alice, bob])
    db_session.commit()
    active = [alice]
    provider = Mock()
    provider.history.return_value = [{'role': 'user', 'content': 'private'}]
    app.dependency_overrides[get_current_user] = lambda: active[0]
    app.dependency_overrides[assistant_client] = lambda: provider
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, active=active, alice=alice, bob=bob, provider=provider)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(assistant_client, None)


def test_conversation_is_owned_and_admin_cannot_read_another_users_history(setup):
    response = setup.client.post('/api/assistant/conversations', json={'title': 'Private conversation', 'rag_enabled': True})
    assert response.status_code == 201
    conversation = response.json()
    assert conversation['status'] == 'ready'
    assert 'owner_id' not in conversation
    identifier = conversation['id']
    setup.provider.ensure_thread.assert_called_once_with(setup.alice.id, identifier, 'Private conversation', True)
    assert setup.client.get(f'/api/assistant/conversations/{identifier}/messages').json()['messages'][0]['content'] == 'private'
    setup.provider.reset_mock()
    setup.active[0] = setup.bob
    assert setup.client.get('/api/assistant/conversations').json() == []
    for method, suffix in [('get', '/messages'), ('post', '/prepare'), ('delete', '')]:
        result = getattr(setup.client, method)(f'/api/assistant/conversations/{identifier}' + suffix)
        assert result.status_code == 404
    setup.provider.history.assert_not_called()
    setup.provider.ensure_thread.assert_not_called()
    setup.provider.delete_thread.assert_not_called()


def test_failed_provisioning_is_retained_and_retry_uses_same_id(setup, db_session):
    setup.provider.ensure_thread.side_effect = AnythingLLMError('secret upstream response')
    response = setup.client.post('/api/assistant/conversations', json={'title': 'Retry me', 'rag_enabled': False})
    assert response.status_code == 503
    assert 'secret' not in response.text
    identifier = response.json()['detail']['conversation_id']
    assert db_session.get(AssistantConversation, identifier).status == 'error'
    setup.provider.ensure_thread.side_effect = None
    response = setup.client.post(f'/api/assistant/conversations/{identifier}/prepare')
    assert response.status_code == 200
    assert response.json()['status'] == 'ready'
    assert response.json()['rag_enabled'] is False


def test_delete_only_removes_local_record_after_upstream_success(setup):
    identifier = setup.client.post('/api/assistant/conversations', json={'title': 'Delete me'}).json()['id']
    setup.provider.delete_thread.side_effect = AnythingLLMError('upstream unavailable')
    assert setup.client.delete(f'/api/assistant/conversations/{identifier}').status_code == 503
    assert len(setup.client.get('/api/assistant/conversations').json()) == 1
    setup.provider.delete_thread.side_effect = None
    assert setup.client.delete(f'/api/assistant/conversations/{identifier}').status_code == 204
    assert setup.client.get('/api/assistant/conversations').json() == []


def test_blank_title_and_unbounded_pagination_are_rejected(setup):
    assert setup.client.post('/api/assistant/conversations', json={'title': '   '}).status_code == 422
    assert setup.client.get('/api/assistant/conversations?limit=10000').status_code == 422
    setup.provider.ensure_thread.assert_not_called()


def test_send_enforces_ownership_and_scheduler_before_upstream(setup, db_session):
    from app.db.models import AssistantComputeGate
    identifier = setup.client.post('/api/assistant/conversations', json={'title': 'Chat'}).json()['id']
    url = f'/api/assistant/conversations/{identifier}/messages'
    setup.active[0] = setup.bob
    assert setup.client.post(url, json={'message': 'hello'}).status_code == 404
    setup.active[0] = setup.alice
    assert setup.client.post(url, json={'message': 'hello'}).status_code == 409
    setup.provider.stream_chat.assert_not_called()
    db_session.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
    db_session.commit()
    async def stream(*args):
        yield {'event': 'delta', 'text': 'reply'}
        yield {'event': 'done'}
    setup.provider.stream_chat.side_effect = stream
    result = setup.client.post(url, json={'message': 'hello'})
    assert result.status_code == 200
    assert 'reply' in result.text
    assert '"event": "done"' in result.text
    setup.provider.stream_chat.assert_called_once_with(setup.alice.id, identifier, True, 'hello')

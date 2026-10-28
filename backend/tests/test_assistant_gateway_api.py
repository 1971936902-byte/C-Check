import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.models import AssistantComputeGate


@pytest.fixture
def gateway_client(db_session_factory, monkeypatch):
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    monkeypatch.setenv('ASSISTANT_GATEWAY_KEY', 'x' * 40)
    monkeypatch.setenv('RAG_EMBEDDING_BASE_URL', 'http://jina/v1')
    monkeypatch.setenv('RAG_EMBEDDING_MODEL', 'jina-code-embeddings-1.5b')
    monkeypatch.setenv('ASSISTANT_QWEN_BASE_URL', 'http://qwen:8001')
    monkeypatch.setenv('ASSISTANT_QWEN_API_KEY', 'qwen-test-key')
    monkeypatch.setenv('ASSISTANT_CONTROL_KEY', 'c' * 40)
    get_settings.cache_clear()
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    from app.assistant_gateway import app
    with TestClient(app) as client:
        def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'embedding-epoch'})
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'epoch': 'embedding-epoch', 'idle': True})
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}]})
        app.state.embedding.transport = httpx.MockTransport(handler)
        yield client


BODY = {'model': 'jina-code-embeddings-1.5b', 'input': ['test']}
AUTH = {'Authorization': 'Bearer ' + 'x' * 40}


def test_internal_endpoint_requires_separate_key(gateway_client):
    assert gateway_client.post('/v1/embeddings', json=BODY).status_code == 401
    assert gateway_client.post('/v1/embeddings', headers={'Authorization': 'Bearer user-jwt'}, json=BODY).status_code == 401
    result = gateway_client.post('/v1/embeddings', headers=AUTH, json=BODY)
    assert result.status_code == 200
    assert result.json()['data'][0]['embedding'] == [1, 0]


def test_internal_endpoint_rejects_model_override_and_batch(gateway_client):
    for invalid in ({**BODY, 'model': 'other'}, {**BODY, 'input': ['a', 'b']}):
        assert gateway_client.post('/v1/embeddings', headers=AUTH, json=invalid).status_code == 422


def test_busy_gateway_does_not_forward(gateway_client, db_session_factory):
    from app.services.assistant_gate import claim
    with db_session_factory() as db:
        claim(db, 'chat')
        db.commit()
    result = gateway_client.post('/v1/embeddings', headers=AUTH, json=BODY)
    assert result.status_code == 409
    assert result.json()['detail'] == 'assistant_busy'


def test_timeout_does_not_leak_upstream_details(gateway_client, db_session_factory):
    from app.assistant_gateway import app
    def timeout(request):
        if request.url.path.endswith('/reserve'):
            return httpx.Response(200, json={'epoch': 'embedding-epoch'})
        raise httpx.ReadTimeout('private upstream details', request=request)
    app.state.embedding.transport = httpx.MockTransport(timeout)
    result = gateway_client.post('/v1/embeddings', headers=AUTH, json=BODY)
    assert result.status_code == 503
    assert 'private' not in result.text
    with db_session_factory() as db:
        assert db.get(AssistantComputeGate, 1).fault is not None


@pytest.mark.parametrize('stream', [True, False])
def test_chat_stream_and_completion_use_same_control(gateway_client, stream):
    from app.assistant_gateway import app
    calls = []
    def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/reserve'):
            return httpx.Response(200, json={'epoch': 'epoch-1'})
        if request.url.path.endswith('/cancel'):
            return httpx.Response(200, json={'epoch': 'epoch-1', 'idle': True})
        if request.method == 'DELETE':
            return httpx.Response(200, json={'released': True})
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n')
    app.state.chat.transport = httpx.MockTransport(upstream)
    response = gateway_client.post('/v1/chat/completions', headers=AUTH,
        json={'model': 'qwen2.5-coder-32b-instruct-fp8',
              'messages': [{'role': 'user', 'content': 'hello'}], 'stream': stream})
    assert response.status_code == 200
    if stream:
        assert 'data: [DONE]' in response.text
    else:
        assert response.json()['choices'][0]['message']['content'] == 'hello'
    assert any(path.endswith('/cancel') for path in calls)

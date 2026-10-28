import json
import asyncio
from uuid import uuid4

import httpx
import pytest

from app.services.anythingllm import AnythingLLM, AnythingLLMError, workspace_slug


def test_workspace_identity_is_user_scoped_and_rag_is_separate():
    alice, bob = str(uuid4()), str(uuid4())
    assert workspace_slug(alice) != workspace_slug(bob)
    assert workspace_slug(alice, True) != workspace_slug(alice, False)
    with pytest.raises(ValueError):
        workspace_slug('../other-user')


def test_thread_creation_uses_fixed_model_and_server_generated_paths():
    owner, conversation = str(uuid4()), str(uuid4())
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer internal-key'
        if request.method == 'GET':
            return httpx.Response(404)
        data = json.loads(request.content)
        if request.url.path == '/api/v1/workspace/new':
            assert data['chatModel'] == 'qwen2.5-coder-32b-instruct-fp8'
            return httpx.Response(200, json={'workspace': {'slug': data['name']}})
        return httpx.Response(200, json={'thread': {'slug': data['slug']}})
    client = AnythingLLM('http://anythingllm', 'internal-key', transport=httpx.MockTransport(handler))
    client.ensure_thread(owner, conversation, 'User title ../ignored', True)
    assert len(requests) == 4
    assert all('ignored' not in str(request.url) for request in requests)


def test_existing_thread_does_not_create_duplicate():
    owner = str(uuid4())
    def handler(request):
        assert request.method == 'GET'
        if request.url.path.endswith('/chats'):
            return httpx.Response(200, json={'history': []})
        return httpx.Response(200, json={'workspace': [{'slug': workspace_slug(owner)}]})
    AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(handler)).ensure_thread(owner, str(uuid4()), 'existing', True)


@pytest.mark.parametrize('status,content', [(200, 'OK'), (204, ''), (404, 'Not Found')])
def test_delete_accepts_provider_plain_text_success_and_missing_thread(status, content):
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=content)))
    client.delete_thread(str(uuid4()), str(uuid4()), True)


def test_delete_does_not_ignore_provider_failure():
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(500, text='private diagnostics')))
    with pytest.raises(AnythingLLMError, match='assistant service unavailable'):
        client.delete_thread(str(uuid4()), str(uuid4()), True)


def test_wrong_workspace_identity_fails_closed():
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'workspace': [{'slug': 'someone-else'}]})))
    with pytest.raises(AnythingLLMError, match='identity mismatch'):
        client.ensure_workspace(str(uuid4()), True)


@pytest.mark.parametrize('payload', [[], 'internal detail', 42, {'workspace': [None]},
    {'workspace': ['invalid']}, {'workspace': 'invalid'}])
def test_malformed_workspace_response_is_a_provider_error(payload):
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(AnythingLLMError):
        client.ensure_workspace(str(uuid4()), True)


@pytest.mark.parametrize('documents', [None, {}, ['invalid'], [{'docpath': []}]])
def test_malformed_embedding_confirmation_is_not_success(documents):
    owner = str(uuid4())
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'workspace': [
            {'slug': workspace_slug(owner), 'documents': documents}]})))
    with pytest.raises(AnythingLLMError):
        client.embed_documents(owner, ['custom-documents/probe.json'])


def test_stream_filters_internal_metadata_and_errors():
    async def scenario():
        content = ('data: ' + json.dumps({'type': 'textResponseChunk', 'textResponse': 'hello',
                   'sources': [{'title': 'my-file', 'text': 'evidence', 'docpath': '/private/storage'}],
                   'close': False}) + '\n\n' + 'data: ' + json.dumps({'type': 'abort',
                   'error': 'internal http://secret review_priority_interrupted', 'close': True}) + '\n\n')
        client = AnythingLLM('http://anythingllm', 'secret', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content)))
        events = [event async for event in client.stream_chat(str(uuid4()), str(uuid4()), True, 'hello')]
        assert events[0] == {'event': 'delta', 'text': 'hello'}
        assert events[-1] == {'event': 'error', 'code': 'review_priority_interrupted'}
        assert '/private' not in json.dumps(events)
        assert 'secret' not in json.dumps(events)
        assert not any(event['event'] == 'done' for event in events)
    asyncio.run(scenario())


def test_truncated_stream_is_not_reported_as_success():
    async def scenario():
        client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content='')))
        events = [event async for event in client.stream_chat(str(uuid4()), str(uuid4()), False, 'hello')]
        assert events == [{'event': 'error', 'code': 'assistant_stream_incomplete'}]
    asyncio.run(scenario())


def test_history_only_exposes_public_message_fields():
    history = [
        {'role': 'user', 'content': 'question', 'attachments': [{'path': '/private'}]},
        {'role': 'assistant', 'content': 'answer', 'metrics': {'internal': 'secret'},
         'sources': [{'title': 'example.c', 'pageContent': 'reference', 'docpath': '/private'},
                     {'title': {'secret': 'key'}, 'text': {'private': 'metadata'}}]},
    ]
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'history': history})))
    assert client.history(str(uuid4()), str(uuid4()), True) == [
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': 'answer', 'sources': [
            {'title': 'example.c', 'text': 'reference'}, {'title': '', 'text': ''}]},
    ]


@pytest.mark.parametrize('message', [None, {'role': 'system', 'content': 'private'},
    {'role': 'assistant', 'content': {}}, {'role': 'assistant', 'content': 'ok', 'sources': {}}])
def test_invalid_history_fails_closed(message):
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'history': [message]})))
    with pytest.raises(AnythingLLMError):
        client.history(str(uuid4()), str(uuid4()), True)


def test_invalid_stream_sources_return_safe_error():
    async def scenario():
        client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content='data: {"sources": {"private": "key"}, "close": true}\n\n')))
        events = [event async for event in client.stream_chat(str(uuid4()), str(uuid4()), True, 'hello')]
        assert events == [{'event': 'error', 'code': 'assistant_service_unavailable'}]
    asyncio.run(scenario())


@pytest.mark.parametrize('reason, expected', [
    ('review_priority_interrupted', 'Interrupted by a higher-priority review.'),
    ('assistant_interrupted', 'Response interrupted before completion.'),
    ('private upstream URL', None),
])
def test_history_preserves_only_recognized_interruption_reasons(reason, expected):
    client = AnythingLLM('http://anythingllm', 'key', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'history': [
            {'role': 'assistant', 'content': 'partial', 'interrupted': reason}]})))
    message = client.history(str(uuid4()), str(uuid4()), True)[0]
    assert message.get('interrupted') == expected

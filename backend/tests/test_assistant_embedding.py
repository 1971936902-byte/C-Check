import asyncio

import httpx
import pytest

from app.db.models import AssistantComputeGate
from app.services import assistant_gate as gate
from app.services.assistant_embedding import AssistantInterrupted, EmbeddingGateway


@pytest.fixture
def sessions(db_session_factory):
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    return db_session_factory


def gateway(sessions, handler, wake=None):
    return EmbeddingGateway(sessions, url='http://jina/v1', api_key='test-key',
        model='jina-code-embeddings-1.5b', heartbeat_interval=.01,
        transport=httpx.MockTransport(handler), wake_reviews=wake)


def test_success_releases_then_wakes_review_queue(sessions):
    woken = []
    async def handler(request):
        assert request.url.path == '/v1/embeddings'
        assert request.headers['Authorization'] == 'Bearer test-key'
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}]})
    def wake():
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
        woken.append(True)
    result = asyncio.run(gateway(sessions, handler, wake).embed(['test']))
    assert result['data'][0]['embedding'] == [1, 0]
    assert woken == [True]


def test_review_preemption_waits_for_inflight_embedding_response(sessions):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await finish.wait()
            return httpx.Response(200, json={'data': []})
        service = gateway(sessions, handler)
        job = asyncio.create_task(service.embed(['test']))
        await started.wait()
        with sessions() as db:
            gate.request_preemption(db)
            db.commit()
            assert not gate.review_may_start(db)
            db.commit()
        await asyncio.sleep(.03)
        assert not job.done()
        finish.set()
        with pytest.raises(AssistantInterrupted):
            await job
        with sessions() as db:
            assert gate.review_may_start(db)
    asyncio.run(scenario())


def test_client_cancellation_does_not_release_inflight_gpu_work(sessions):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await finish.wait()
            return httpx.Response(200, json={'data': []})
        service = gateway(sessions, handler)
        caller = asyncio.create_task(service.embed(['test']))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(service.jobs) == 1
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is not None
        inflight = list(service.jobs)
        finish.set()
        await asyncio.gather(*inflight)
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
    asyncio.run(scenario())


def test_timeout_keeps_ownership_and_requires_recovery(sessions):
    async def handler(request):
        raise httpx.ReadTimeout('uncertain upstream outcome', request=request)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(gateway(sessions, handler).embed(['test']))
    with sessions() as db:
        owner = db.get(AssistantComputeGate, 1)
        assert owner.owner_token is not None
        assert owner.fault == 'embedding upstream completion unconfirmed'
        assert not gate.review_may_start(db)


def test_oversized_batch_is_rejected_before_claim(sessions):
    async def handler(request):
        pytest.fail('invalid input must not contact upstream')
    service = gateway(sessions, handler)
    for inputs in ([], ['a', 'b'], [''], ['x' * 16001]):
        with pytest.raises(ValueError):
            asyncio.run(service.embed(inputs))
    with sessions() as db:
        assert db.get(AssistantComputeGate, 1).owner_token is None


def test_existing_root_endpoint_gets_v1_path(sessions):
    async def handler(request):
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'data': []})
    gateway = EmbeddingGateway(sessions, url='http://embedding:8002', api_key='key',
        model='jina-code-embeddings-1.5b', transport=httpx.MockTransport(handler))
    asyncio.run(gateway.embed(['test']))


def test_controlled_embedding_checkpoints_and_releases_after_confirmation(sessions):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.method + ' ' + request.url.path.rsplit('/', 1)[-1])
            assert request.headers['X-CCheck-Control-Key'] == 'control-secret'
            with sessions() as db:
                state = db.get(AssistantComputeGate, 1)
                if request.url.path.endswith('/reserve'):
                    assert state.control_token == request.url.path.split('/')[-2]
                    return httpx.Response(200, json={'epoch': 'jina-one'})
                if request.url.path.endswith('/embeddings'):
                    assert state.operation == 'embedding' and state.control_epoch == 'jina-one'
                    assert request.url.params['epoch'] == 'jina-one'
                    return httpx.Response(200, json={'data': [{'embedding': [1, 0]}]})
                if request.url.path.endswith('/cancel'):
                    return httpx.Response(200, json={'epoch': 'jina-one', 'idle': True})
                assert request.method == 'DELETE'
                return httpx.Response(200, json={})
        service = EmbeddingGateway(sessions, url='http://jina/v1', api_key='key', model='jina',
            control_key='control-secret', transport=httpx.MockTransport(handler))
        assert (await service.embed(['test']))['data'][0]['embedding'] == [1, 0]
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
        assert calls[0] == 'POST reserve'
        assert calls[-1].startswith('DELETE ')
    asyncio.run(scenario())


@pytest.mark.parametrize('matching_epoch', [True, False])
def test_embedding_timeout_recovery_requires_matching_engine_idle(sessions, matching_epoch):
    async def scenario():
        completed = False
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'jina-one'})
            if request.url.path.endswith('/embeddings'):
                raise httpx.ReadTimeout('simulated lost response', request=request)
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'epoch': 'jina-one' if matching_epoch else 'other', 'idle': completed})
            return httpx.Response(200, json={})
        service = EmbeddingGateway(sessions, url='http://jina', api_key='key', model='jina',
            control_key='control-secret', transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.ReadTimeout):
            await service.embed(['test'])
        with sessions() as db:
            state = db.get(AssistantComputeGate, 1)
            assert state.owner_token and state.control_epoch == 'jina-one'
            assert not gate.review_may_start(db)
            db.commit()
        assert not await service.recover_once()
        completed = True
        assert await service.recover_once() is matching_epoch
        with sessions() as db:
            assert gate.review_may_start(db) is matching_epoch
    asyncio.run(scenario())

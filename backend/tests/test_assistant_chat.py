import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.models import AssistantComputeGate
from app.services import assistant_gate as gate
from app.services.assistant_chat import ChatGateway


@pytest.fixture
def sessions(db_session_factory):
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    return db_session_factory


PAYLOAD = {'model': 'qwen2.5-coder-32b-instruct-fp8',
           'messages': [{'role': 'user', 'content': 'hello'}]}
CHUNK = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'


def service(sessions, handler, **kwargs):
    return ChatGateway(sessions, url='http://qwen', api_key='test', control_key='control',
        heartbeat_interval=.01, transport=httpx.MockTransport(handler), **kwargs)


class PendingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
    async def __aiter__(self):
        yield CHUNK
        await asyncio.Event().wait()
    async def aclose(self):
        self.closed = True


def test_recovery_during_reservation_blocks_late_inference(sessions):
    async def scenario():
        reservation_started, respond = asyncio.Event(), asyncio.Event()
        woke = []
        requests = []
        async def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path.endswith('/reserve'):
                reservation_started.set()
                await respond.wait()
                return httpx.Response(200, json={'epoch': 'late-epoch'})
            if request.method == 'DELETE':
                return httpx.Response(200, json={'released': True})
            raise AssertionError('revoked work must never submit inference')
        gateway = service(sessions, handler, wake_reviews=lambda: woke.append(True))
        stream = await gateway.start(PAYLOAD)
        await reservation_started.wait()
        with sessions() as db:
            state = db.get(AssistantComputeGate, 1)
            gate.fail_closed(db, state.owner_token, 'gateway recovery test')
            db.commit()
        assert await gateway.recover_once()
        assert woke
        respond.set()
        output = b''.join([chunk async for chunk in stream])
        assert b'[DONE]' not in output
        assert all(path.endswith('/reserve') or method == 'DELETE' for method, path in requests)
        with sessions() as db:
            assert gate.status(db)['available']
    asyncio.run(scenario())


def test_normal_completion_requires_engine_ack_before_done(sessions):
    async def scenario():
        ack_started, ack_allowed = asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                with sessions() as db:
                    state = db.get(AssistantComputeGate, 1)
                    assert state.control_token == request.url.path.split('/')[-2]
                    assert state.control_epoch is None
                return httpx.Response(200, json={'epoch': 'epoch-1'})
            if request.url.path.endswith('/cancel'):
                ack_started.set()
                await ack_allowed.wait()
                return httpx.Response(200, json={'epoch': 'epoch-1', 'idle': True})
            if request.method == 'DELETE':
                with sessions() as db:
                    assert db.get(AssistantComputeGate, 1).owner_token is None
                return httpx.Response(200, json={'released': True})
            with sessions() as db:
                assert db.get(AssistantComputeGate, 1).control_epoch == 'epoch-1'
            return httpx.Response(200, content=CHUNK + b'data: [DONE]\n\n')
        gateway = service(sessions, handler)
        stream = await gateway.start(PAYLOAD)
        first = await anext(stream)
        assert b'hello' in first
        await ack_started.wait()
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is not None
        ack_allowed.set()
        remainder = b''.join([chunk async for chunk in stream])
        assert remainder == b'data: [DONE]\n\n'
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
    asyncio.run(scenario())


def test_finish_reason_is_not_forwarded_before_engine_confirmation(sessions):
    async def scenario():
        ack_started, ack_allowed = asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'one'})
            if request.url.path.endswith('/cancel'):
                ack_started.set()
                await ack_allowed.wait()
                return httpx.Response(200, json={'epoch': 'one', 'idle': True})
            if request.method == 'DELETE':
                return httpx.Response(200, json={})
            return httpx.Response(200, content=CHUNK + b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        stream = await service(sessions, handler).start(PAYLOAD)
        assert b'hello' in await anext(stream)
        terminal = asyncio.create_task(anext(stream))
        await ack_started.wait()
        assert not terminal.done()
        ack_allowed.set()
        assert b'finish_reason' in await terminal
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
        await stream.aclose()
    asyncio.run(scenario())


def test_failed_database_release_preserves_engine_recovery_record(sessions):
    async def scenario():
        async def handler(request):
            assert request.method != 'DELETE', 'idle proof must survive a failed database release'
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'one'})
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'epoch': 'one', 'idle': True})
            return httpx.Response(200, content=b'data: [DONE]\n\n')
        gateway = service(sessions, handler)
        transaction = gateway._transaction

        def failing_release(function, *args, **kwargs):
            if function is gate.acknowledge_idle:
                raise RuntimeError('database temporarily unavailable')
            return transaction(function, *args, **kwargs)

        gateway._transaction = failing_release
        stream = await gateway.start(PAYLOAD)
        with pytest.raises(RuntimeError, match='database temporarily unavailable'):
            async for _ in stream:
                pass
        with sessions() as db:
            state = db.get(AssistantComputeGate, 1)
            assert state.owner_token is not None
            assert state.control_epoch == 'one'
            assert state.control_token is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('disconnect', [False, True])
def test_review_or_disconnect_cancels_upstream_and_waits_for_ack(sessions, disconnect):
    async def scenario():
        ack_started, ack_allowed = asyncio.Event(), asyncio.Event()
        upstream = PendingStream()
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'epoch-1'})
            if request.url.path.endswith('/cancel'):
                assert upstream.closed
                ack_started.set()
                await ack_allowed.wait()
                return httpx.Response(200, json={'epoch': 'epoch-1', 'idle': True})
            if request.method == 'DELETE':
                return httpx.Response(200, json={'released': True})
            return httpx.Response(200, stream=upstream)
        gateway = service(sessions, handler)
        stream = await gateway.start(PAYLOAD)
        assert b'hello' in await anext(stream)
        if disconnect:
            await stream.aclose()
        else:
            with sessions() as db:
                gate.request_preemption(db)
                db.commit()
        await asyncio.wait_for(ack_started.wait(), 2)
        with sessions() as db:
            assert not gate.review_may_start(db)
            db.commit()
        jobs = list(gateway.jobs)
        ack_allowed.set()
        if not disconnect:
            remainder = b''.join([chunk async for chunk in stream])
            assert b'review_priority_interrupted' in remainder
            assert b'[DONE]' not in remainder
        await asyncio.gather(*jobs)
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is None
    asyncio.run(scenario())


def test_engine_epoch_mismatch_keeps_compute_fenced(sessions):
    async def scenario():
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'old'})
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'epoch': 'new', 'idle': True})
            return httpx.Response(200, content=CHUNK + b'data: [DONE]\n\n')
        stream = await service(sessions, handler).start(PAYLOAD)
        result = b''.join([chunk async for chunk in stream])
        assert b'assistant_recovery_required' in result
        assert b'[DONE]' not in result
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token is not None
            assert not gate.review_may_start(db)
    asyncio.run(scenario())


def test_unacknowledged_abort_times_out_without_releasing(sessions):
    async def scenario():
        async def handler(request):
            if request.url.path.endswith('/reserve'):
                return httpx.Response(200, json={'epoch': 'one'})
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'epoch': 'one', 'idle': False})
            return httpx.Response(200, content=b'data: [DONE]\n\n')
        stream = await service(sessions, handler, drain_timeout=.02).start(PAYLOAD)
        result = b''.join([chunk async for chunk in stream])
        assert b'assistant_recovery_required' in result
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).fault is not None
    asyncio.run(scenario())


def abandoned_chat(sessions, *, expired=True):
    with sessions() as db:
        token = gate.claim(db, 'chat')
        gate.checkpoint_upstream(db, token, 'a' * 64, 'old-engine')
        if expired:
            db.get(AssistantComputeGate, 1).heartbeat_at = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5))
        db.commit()
    return token


def test_restarted_gateway_recovers_only_after_matching_engine_idle(sessions):
    abandoned_chat(sessions)
    woken = []
    async def scenario():
        async def handler(request):
            if request.method == 'DELETE':
                return httpx.Response(200, json={'released': True})
            assert request.url.path.endswith('/' + 'a' * 64 + '/cancel')
            assert request.url.params['epoch'] == 'old-engine'
            with sessions() as db:
                assert not gate.review_may_start(db)
                assert not gate.heartbeat(db, db.get(AssistantComputeGate, 1).owner_token)
                db.commit()
            return httpx.Response(200, json={'epoch': 'old-engine', 'idle': True})
        assert await service(sessions, handler, wake_reviews=lambda: woken.append(True)).recover_once()
        with sessions() as db:
            assert gate.review_may_start(db)
            assert db.get(AssistantComputeGate, 1).control_token is None
    asyncio.run(scenario())
    assert woken == [True]


@pytest.mark.parametrize('result', [
    {'epoch': 'new-engine', 'idle': True}, {'epoch': 'old-engine', 'idle': False},
    {'epoch': 'old-engine', 'idle': 'true'}, [],
])
def test_recovery_never_releases_without_unambiguous_confirmation(sessions, result):
    token = abandoned_chat(sessions)
    async def scenario():
        async def handler(request):
            assert request.method == 'POST'
            return httpx.Response(200, json=result)
        assert not await service(sessions, handler).recover_once()
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token == token
            assert not gate.review_may_start(db)
    asyncio.run(scenario())


def test_recovery_does_not_interrupt_healthy_chat(sessions):
    abandoned_chat(sessions, expired=False)
    async def scenario():
        async def handler(request):
            pytest.fail('healthy chat must not be contacted by recovery')
        assert not await service(sessions, handler).recover_once()
        with sessions() as db:
            state = db.get(AssistantComputeGate, 1)
            assert not state.cancel_requested and state.fault is None
    asyncio.run(scenario())


def test_late_recovery_ack_cannot_release_new_owner(sessions):
    token = abandoned_chat(sessions)
    new_owner = []
    async def scenario():
        async def handler(request):
            if request.method == 'DELETE':
                return httpx.Response(200, json={})
            with sessions() as db:
                # Another recovery already confirmed the old request and released it.
                assert gate.acknowledge_idle(db, token)
                db.commit()
            with sessions() as db:
                new_owner.append(gate.claim(db, 'chat'))
                db.commit()
            return httpx.Response(200, json={'epoch': 'old-engine', 'idle': True})
        assert not await service(sessions, handler).recover_once()
        with sessions() as db:
            assert db.get(AssistantComputeGate, 1).owner_token == new_owner[0]
    asyncio.run(scenario())

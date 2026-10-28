import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


path = Path(__file__).resolve().parents[2] / 'deploy/assistant/vllm_control.py'
spec = importlib.util.spec_from_file_location('ccheck_vllm_control_test', path)
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)
TOKEN = 'a' * 64


def test_cancel_before_admission_fences_late_request():
    async def scenario():
        manager = control.Controller()
        request_id = manager.reserve(TOKEN)
        assert await manager.cancel(TOKEN, manager.epoch)
        with pytest.raises(ValueError, match='not admitted'):
            manager.admit(request_id, object())
        manager.forget(TOKEN, manager.epoch)
        with pytest.raises(ValueError, match='not admitted'):
            manager.admit(request_id, object())
    asyncio.run(scenario())


def test_abort_enqueue_does_not_acknowledge_scheduler_completion():
    async def scenario():
        manager = control.Controller()
        request_id = manager.reserve(TOKEN)
        enqueued = asyncio.Event()
        class Engine:
            _request_tracker = SimpleNamespace(new_requests_event=asyncio.Event())
            async def abort(self, value):
                assert value == request_id
                enqueued.set()
        engine = Engine()
        manager.admit(request_id, engine)
        pending = asyncio.create_task(manager.cancel(TOKEN, manager.epoch, timeout=1))
        await enqueued.wait()
        await asyncio.sleep(0)
        assert not pending.done()
        assert engine._request_tracker.new_requests_event.is_set()
        with pytest.raises(ValueError, match='unconfirmed'):
            manager.forget(TOKEN, manager.epoch)
        manager.acknowledge_scheduler_abort([request_id])
        assert await pending
        manager.forget(TOKEN, manager.epoch)
    asyncio.run(scenario())


def test_timeout_and_epoch_mismatch_do_not_confirm_idle():
    async def scenario():
        manager = control.Controller()
        request_id = manager.reserve(TOKEN)
        async def abort(value):
            pass
        manager.admit(request_id, SimpleNamespace(abort=abort,
            _request_tracker=SimpleNamespace(new_requests_event=asyncio.Event())))
        assert not await manager.cancel(TOKEN, manager.epoch, timeout=.001)
        with pytest.raises(ValueError, match='epoch'):
            await manager.cancel(TOKEN, 'old-process')
        with pytest.raises(ValueError, match='unconfirmed'):
            manager.forget(TOKEN, manager.epoch)
    asyncio.run(scenario())


def test_normal_review_ids_are_not_modified():
    manager = control.Controller()
    manager.admit('chatcmpl-review-id', object())
    assert manager.requests == {}


def test_invalid_duplicate_and_capacity_reservations_fail_closed():
    manager = control.Controller(capacity=1)
    with pytest.raises(ValueError):
        manager.reserve('invalid')
    manager.reserve(TOKEN)
    with pytest.raises(ValueError, match='already reserved'):
        manager.reserve(TOKEN)
    with pytest.raises(ValueError, match='capacity'):
        manager.reserve('b' * 64)


def test_plain_requests_skip_schema_preparation():
    async def scenario():
        params = SimpleNamespace(guided_decoding=None)
        async def unexpected(**kwargs):
            raise AssertionError('plain chat must not compile a schema')
        assert await control.prepare_guided_params(None, params, None, unexpected) is params
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['complete', 'cancel', 'error'])
def test_guided_compilation_precedes_tracker_and_rechecks_cancellation(monkeypatch, outcome):
    from fastapi import APIRouter

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        compiled = SimpleNamespace(guided_decoding=None)
        schema = {'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 1}}}
        params = SimpleNamespace(guided_decoding=schema)
        queued = []
        async def tokenizer(lora):
            assert lora == 'adapter'
            return 'tokenizer'
        async def build(**kwargs):
            assert kwargs['sampling_params'] is params
            assert kwargs['sampling_params'].guided_decoding == schema
            assert kwargs['tokenizer'] == 'tokenizer'
            started.set()
            await finish.wait()
            if outcome == 'error':
                raise ValueError('schema compilation failed')
            return compiled
        class Engine:
            def __init__(self):
                self.engine = SimpleNamespace(
                    get_tokenizer_async=tokenizer,
                    decoding_config=SimpleNamespace(guided_decoding_backend='xgrammar', reasoning_backend=None),
                    model_config=object(), parallel_config=SimpleNamespace(pipeline_parallel_size=1))
            async def add_request(self, request_id, prompt=None, params=None, lora_request=None):
                queued.append((request_id, prompt, params, lora_request))
                return 'stream'
            async def _engine_abort(self, ids):
                pass
        class Tracker:
            def get_new_and_aborted_requests(self):
                return [], set()
        monkeypatch.setitem(sys.modules, 'vllm.engine.async_llm_engine', SimpleNamespace(
            AsyncLLMEngine=Engine, RequestTracker=Tracker,
            build_guided_decoding_logits_processor_async=build))
        monkeypatch.setattr(control.importlib.metadata, 'version', lambda name: '0.8.5.post1')
        manager = control.install(SimpleNamespace(router=APIRouter()), 'k' * 32)
        request_id = manager.reserve(TOKEN)
        engine = Engine()
        task = asyncio.create_task(engine.add_request(request_id, 'prompt', params, lora_request='adapter'))
        await started.wait()
        assert not queued and not manager.requests[request_id].admitted
        if outcome == 'cancel':
            assert await manager.cancel(TOKEN, manager.epoch)
        finish.set()
        if outcome == 'error':
            with pytest.raises(ValueError, match='schema compilation failed'):
                await task
            assert not queued and not manager.requests[request_id].admitted
        elif outcome == 'cancel':
            with pytest.raises(ValueError, match='not admitted'):
                await task
            assert not queued
        else:
            assert await task == 'stream'
            assert queued == [(request_id, 'prompt', compiled, 'adapter')]
    asyncio.run(scenario())

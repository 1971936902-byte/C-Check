"""CPU-only contract probe, run inside the pinned inference image."""
import asyncio
from types import SimpleNamespace

from fastapi import APIRouter
from vllm.engine.async_llm_engine import AsyncLLMEngine, RequestTracker
from vllm.sampling_params import SamplingParams

from vllm_control import install


async def main():
    control = install(SimpleNamespace(router=APIRouter()), 'probe-only-' + 'x' * 32)
    removed = []
    engine = SimpleNamespace(is_running=True, log_requests=False,
        engine=SimpleNamespace(parallel_config=SimpleNamespace(pipeline_parallel_size=1),
            scheduler_config=SimpleNamespace(policy='fcfs'),
            abort_request=lambda ids: removed.extend(ids)),
        _request_tracker=RequestTracker())
    engine._abort = lambda request_id: AsyncLLMEngine._abort(engine, request_id)
    async def abort(request_id):
        await AsyncLLMEngine.abort(engine, request_id)
    engine.abort = abort

    for token, already_scheduled in [('a' * 64, False), ('b' * 64, True)]:
        request_id = control.reserve(token)
        await AsyncLLMEngine.add_request(engine, request_id, prompt='test', params=SamplingParams(max_tokens=1))
        if already_scheduled:
            new, aborted = engine._request_tracker.get_new_and_aborted_requests()
            assert len(new) == 1 and not aborted
        cancellation = asyncio.create_task(control.cancel(token, control.epoch, timeout=1))
        await asyncio.sleep(0)
        assert not cancellation.done()
        assert engine._request_tracker.new_requests_event.is_set()
        new, aborted = engine._request_tracker.get_new_and_aborted_requests()
        assert not new
        if already_scheduled:
            assert aborted == {request_id}
            assert not control.requests[request_id].drained.is_set()
            await AsyncLLMEngine._engine_abort(engine, aborted)
            assert request_id in removed
        else:
            assert not aborted
            assert control.requests[request_id].drained.is_set()
        assert await cancellation
        control.forget(token, control.epoch)
        try:
            await AsyncLLMEngine.add_request(engine, request_id, prompt='late', params=SamplingParams(max_tokens=1))
        except ValueError:
            pass
        else:
            raise AssertionError('late request was admitted')
    print('VLLM_CONTROL_CPU_CONTRACT_OK')


asyncio.run(main())

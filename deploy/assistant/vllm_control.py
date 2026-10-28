"""Cancellation acknowledgements for the pinned vLLM V0 single-pipeline engine.

This module is installed by the assistant-enabled launcher only. Normal review
request IDs are untouched. Assistant IDs require an explicit reservation before
they may reach the engine tracker; deleting a reservation fences late arrivals.
"""
import asyncio
from dataclasses import dataclass, field
import importlib.metadata
import inspect
import re
import secrets

from fastapi import APIRouter, Header, HTTPException


PREFIX = 'chatcmpl-ccheck-assistant-'
TOKEN = re.compile(r'^[a-f0-9]{64}$')


async def prepare_guided_params(engine, params, lora_request, build):
    # V0 compiles inside engine_step by default. Cold schema compilation must
    # not consume the engine iteration watchdog and kill unrelated requests.
    if getattr(params, 'guided_decoding', None) is None:
        return params
    inner = engine.engine
    return await build(
        sampling_params=params,
        tokenizer=await inner.get_tokenizer_async(lora_request),
        default_guided_backend=inner.decoding_config.guided_decoding_backend,
        reasoning_backend=inner.decoding_config.reasoning_backend,
        model_config=inner.model_config,
    )


@dataclass
class Reservation:
    cancelled: bool = False
    admitted: bool = False
    engine: object | None = None
    drained: asyncio.Event = field(default_factory=asyncio.Event)


class Controller:
    def __init__(self, capacity=1024):
        self.epoch = secrets.token_hex(16)
        self.requests: dict[str, Reservation] = {}
        self.capacity = capacity

    def reserve(self, token):
        if not TOKEN.fullmatch(token):
            raise ValueError('invalid request token')
        request_id = PREFIX + token
        if request_id in self.requests:
            raise ValueError('request token already reserved')
        if len(self.requests) >= self.capacity:
            raise ValueError('control capacity exhausted')
        self.requests[request_id] = Reservation()
        return request_id

    def admit(self, request_id, engine):
        if not request_id.startswith(PREFIX):
            return
        state = self.requests.get(request_id)
        if state is None or state.cancelled or state.admitted:
            raise ValueError('assistant request is not admitted')
        state.admitted = True
        state.engine = engine

    def acknowledge_scheduler_abort(self, request_ids):
        for request_id in request_ids:
            state = self.requests.get(request_id)
            if state is not None and state.cancelled:
                state.drained.set()

    async def cancel(self, token, epoch, timeout=10):
        if epoch != self.epoch:
            raise ValueError('engine epoch changed')
        state = self.requests.get(PREFIX + token)
        if state is None:
            raise ValueError('unknown request reservation')
        state.cancelled = True
        if not state.admitted:
            # Admission and this state change have no awaits between them.
            state.drained.set()
        elif not state.drained.is_set():
            await state.engine.abort(PREFIX + token)
            # vLLM's abort queue alone does not wake an idle engine loop.
            state.engine._request_tracker.new_requests_event.set()
        try:
            await asyncio.wait_for(state.drained.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def forget(self, token, epoch):
        state = self.requests.get(PREFIX + token)
        if epoch != self.epoch or state is None or not state.drained.is_set():
            raise ValueError('cannot release an unconfirmed request')
        del self.requests[PREFIX + token]


def install(api_server, control_key):
    """Call before run_server; launch with V0, frontend multiprocessing disabled,
    pipeline_parallel_size=1. Version drift must fail closed, never silently run.
    """
    if importlib.metadata.version('vllm') != '0.8.5.post1':
        raise RuntimeError('assistant cancellation requires pinned vLLM 0.8.5.post1')
    if len(control_key) < 32:
        raise RuntimeError('dedicated control key must have at least 32 characters')
    from vllm.engine.async_llm_engine import (
        AsyncLLMEngine, RequestTracker, build_guided_decoding_logits_processor_async,
    )
    controller = Controller()
    original_add = AsyncLLMEngine.add_request
    original_abort = AsyncLLMEngine._engine_abort
    original_drain = RequestTracker.get_new_and_aborted_requests
    add_signature = inspect.signature(original_add)

    async def add_request(engine, request_id, *args, **kwargs):
        bound = add_signature.bind(engine, request_id, *args, **kwargs)
        bound.apply_defaults()
        bound.arguments['params'] = await prepare_guided_params(
            engine, bound.arguments['params'], bound.arguments['lora_request'],
            build_guided_decoding_logits_processor_async,
        )
        if request_id.startswith(PREFIX):
            if engine.engine.parallel_config.pipeline_parallel_size != 1:
                raise ValueError('assistant control requires one pipeline stage')
            controller.admit(request_id, engine)
        # Admission is checked after compilation so a cancellation during CPU
        # preparation still fences the request before it can reach the GPU.
        return await original_add(*bound.args, **bound.kwargs)

    async def engine_abort(engine, request_ids):
        request_ids = tuple(request_ids)
        await original_abort(engine, request_ids)
        # This hook runs inside the engine loop, after the preceding model step
        # and after removal from the scheduler, not when abort was merely queued.
        controller.acknowledge_scheduler_abort(request_ids)

    def drain_tracker(tracker):
        pending_aborts = set(tracker._aborted_requests._queue)
        new, aborted = original_drain(tracker)
        # vLLM removes an ID from the abort set when it cancels a request still
        # in the new-request queue. Such requests never entered the scheduler.
        controller.acknowledge_scheduler_abort(pending_aborts - set(aborted))
        return new, aborted

    AsyncLLMEngine.add_request = add_request
    AsyncLLMEngine._engine_abort = engine_abort
    RequestTracker.get_new_and_aborted_requests = drain_tracker
    router = APIRouter(prefix='/internal/assistant')

    def auth(value):
        if not value or not secrets.compare_digest(value.encode(), control_key.encode()):
            raise HTTPException(401, 'invalid control credentials')

    @router.post('/{token}/reserve')
    async def reserve(token: str, x_ccheck_control_key: str | None = Header(default=None)):
        auth(x_ccheck_control_key)
        try:
            request_id = controller.reserve(token)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'epoch': controller.epoch, 'request_id': request_id}

    @router.post('/{token}/cancel')
    async def cancel(token: str, epoch: str, x_ccheck_control_key: str | None = Header(default=None)):
        auth(x_ccheck_control_key)
        try:
            idle = await controller.cancel(token, epoch)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'epoch': controller.epoch, 'idle': idle}

    @router.delete('/{token}')
    async def forget(token: str, epoch: str, x_ccheck_control_key: str | None = Header(default=None)):
        auth(x_ccheck_control_key)
        try:
            controller.forget(token, epoch)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'released': True}

    api_server.router.include_router(router)
    return controller

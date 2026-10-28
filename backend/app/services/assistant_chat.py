"""Streaming chat admission and cancellation with engine-side acknowledgement."""
import asyncio
import json
import logging
import secrets
import time

import httpx

from app.services import assistant_gate as gate


class ChatStreamError(RuntimeError):
    pass


async def collect_completion(stream, model):
    content = []
    usage = None
    finish_reason = 'stop'
    async for event in stream:
        text = event.decode().strip()
        if not text.startswith('data: ') or text == 'data: [DONE]':
            continue
        data = json.loads(text[6:])
        if 'error' in data:
            raise ChatStreamError(data['error']['code'])
        if data.get('usage') is not None:
            usage = data['usage']
        for choice in data.get('choices', []):
            value = choice.get('delta', {}).get('content')
            if value:
                content.append(value)
            if choice.get('finish_reason'):
                finish_reason = choice['finish_reason']
    return {'id': 'chatcmpl-' + secrets.token_hex(16), 'object': 'chat.completion',
            'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''.join(content)},
                         'finish_reason': finish_reason}], 'usage': usage}


class ChatGateway:
    def __init__(self, sessions, *, url, api_key, control_key, lease_seconds=30,
                 heartbeat_interval=1, timeout=120, drain_timeout=30,
                 transport=None, wake_reviews=None):
        self.sessions = sessions
        self.url = url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {api_key}', 'X-CCheck-Control-Key': control_key}
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout
        self.drain_timeout = drain_timeout
        self.transport = transport
        self.wake_reviews = wake_reviews
        self.jobs = set()

    def _transaction(self, function, *args, **kwargs):
        with self.sessions() as db:
            result = function(db, *args, **kwargs)
            db.commit()
            return result

    async def start(self, payload):
        token = await asyncio.to_thread(self._transaction, gate.claim, 'chat',
                                        lease_seconds=self.lease_seconds)
        output = asyncio.Queue(maxsize=32)
        cancelled = asyncio.Event()
        job = asyncio.create_task(self._run(token, payload, output, cancelled))
        self.jobs.add(job)
        def finished(task):
            self.jobs.discard(task)
            if not task.cancelled():
                task.exception()
        job.add_done_callback(finished)

        async def stream():
            pending = None
            try:
                while not job.done() or not output.empty():
                    pending = asyncio.create_task(output.get())
                    done, _ = await asyncio.wait({pending, job}, return_when=asyncio.FIRST_COMPLETED)
                    if pending in done:
                        yield pending.result()
                    else:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                error, final_chunks = await asyncio.shield(job)
                if error:
                    yield ('data: ' + json.dumps({'error': {'message': error, 'code': error}}) + '\n\n').encode()
                else:
                    for chunk in final_chunks:
                        yield chunk
                    yield b'data: [DONE]\n\n'
            finally:
                cancelled.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                # The tracked job continues cancellation even after ASGI disconnects.
        return stream()

    async def recover_once(self):
        if await asyncio.to_thread(self._transaction, gate.recover_unsubmitted,
                                    lease_seconds=self.lease_seconds):
            if self.wake_reviews is not None:
                await asyncio.to_thread(self.wake_reviews)
            return True
        candidate = await asyncio.to_thread(self._transaction, gate.recovery_candidate,
                                            lease_seconds=self.lease_seconds)
        if candidate is None:
            return False
        token, control_token, epoch = candidate
        path = self.url + '/internal/assistant/' + control_token
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.drain_timeout,
                    transport=self.transport, trust_env=False, follow_redirects=False) as client:
                response = await client.post(path + '/cancel', params={'epoch': epoch})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get('epoch') != epoch or result.get('idle') is not True:
                    return False
                # The cancelled reservation fences any request from a delayed old
                # gateway. Ownership is checked again after the network round trip.
                released = await asyncio.to_thread(self._transaction, gate.acknowledge_idle, token)
                if released and self.wake_reviews is not None:
                    await asyncio.to_thread(self.wake_reviews)
                try:
                    await client.delete(path, params={'epoch': epoch})
                except httpx.HTTPError:
                    logging.getLogger(__name__).warning('Recovered chat reservation cleanup deferred')
                return released
        except (httpx.HTTPError, ValueError):
            return False

    async def _run(self, token, payload, output, cancelled):
        control_token = secrets.token_hex(32)
        control_path = self.url + '/internal/assistant/' + control_token
        epoch = None
        idle = True
        stopped = asyncio.Event()
        pump = None
        error = None
        final_chunks = []

        async def monitor():
            while not stopped.is_set():
                try:
                    allowed = await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                                       lease_seconds=self.lease_seconds)
                except Exception:
                    allowed = False
                if not allowed:
                    cancelled.set()
                    return
                try:
                    await asyncio.wait_for(stopped.wait(), self.heartbeat_interval)
                except TimeoutError:
                    pass

        watcher = asyncio.create_task(monitor())
        cancellation = asyncio.create_task(cancelled.wait())
        try:
            await asyncio.to_thread(self._transaction, gate.checkpoint_upstream, token, control_token)
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout,
                                         transport=self.transport, trust_env=False, follow_redirects=False) as client:
                reservation = await client.post(control_path + '/reserve')
                reservation.raise_for_status()
                epoch = reservation.json()['epoch']
                await asyncio.to_thread(self._transaction, gate.checkpoint_upstream, token, control_token, epoch)
                allowed = await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                                   lease_seconds=self.lease_seconds)
                if not allowed:
                    cancelled.set()

                async def forward():
                    upstream = {**payload, 'request_id': 'ccheck-assistant-' + control_token,
                                'stream': True, 'n': 1, 'stream_options': {'include_usage': True}}
                    async with client.stream('POST', self.url + '/v1/chat/completions', json=upstream) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line == 'data: [DONE]':
                                return
                            if line:
                                event = (line + '\n\n').encode()
                                data = json.loads(line[6:]) if line.startswith('data: ') else {}
                                terminal = any(choice.get('finish_reason') for choice in data.get('choices', []))
                                if terminal or final_chunks:
                                    final_chunks.append(event)
                                else:
                                    await output.put(event)
                    raise RuntimeError('upstream stream ended without completion marker')

                try:
                    if not cancelled.is_set():
                        idle = False
                        pump = asyncio.create_task(forward())
                        done, _ = await asyncio.wait({pump, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                        if cancellation in done:
                            error = 'review_priority_interrupted'
                        else:
                            await pump
                    else:
                        error = 'review_priority_interrupted'
                finally:
                    if pump is not None:
                        pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)
                    # HTTP disconnect alone is insufficient. Even normal completion
                    # is fenced against a late request before ownership is released.
                    async with asyncio.timeout(self.drain_timeout):
                        while True:
                            ack = await client.post(control_path + '/cancel', params={'epoch': epoch})
                            ack.raise_for_status()
                            result = ack.json()
                            if result.get('epoch') != epoch:
                                raise RuntimeError('engine epoch changed')
                            if result.get('idle') is True:
                                idle = True
                                break
                            await asyncio.sleep(.1)
        except Exception:
            error = 'assistant_upstream_unavailable' if idle else 'assistant_recovery_required'
        finally:
            stopped.set()
            cancellation.cancel()
            await asyncio.gather(watcher, cancellation, return_exceptions=True)
            if cancelled.is_set() and error is None:
                error = 'review_priority_interrupted'
            if error or cancelled.is_set():
                while not output.empty():
                    output.get_nowait()
            if idle:
                await asyncio.to_thread(self._transaction, gate.acknowledge_idle, token)
                if self.wake_reviews is not None:
                    try:
                        await asyncio.to_thread(self.wake_reviews)
                    except Exception:
                        logging.getLogger(__name__).exception('Failed to wake review queue after chat')
            else:
                await asyncio.to_thread(self._transaction, gate.fail_closed, token,
                                       'chat upstream cancellation unconfirmed')
        # Preserve the engine's idle proof until the database release commits.
        # A crash before that commit must leave recovery a cancellable reservation.
        if idle and epoch is not None:
            try:
                async with httpx.AsyncClient(headers=self.headers, timeout=self.drain_timeout,
                        transport=self.transport, trust_env=False, follow_redirects=False) as client:
                    response = await client.delete(control_path, params={'epoch': epoch})
                    response.raise_for_status()
            except httpx.HTTPError:
                logging.getLogger(__name__).warning('Assistant control reservation cleanup deferred')
        return error, final_chunks

"""Small-batch assistant embedding with confirmed completion before GPU release."""
import asyncio
import logging
import secrets
from collections.abc import Callable

import httpx

from app.services import assistant_gate as gate


class AssistantInterrupted(RuntimeError):
    pass


class EmbeddingGateway:
    def __init__(self, sessions, *, url: str, api_key: str, model: str,
                 timeout: float = 120, lease_seconds: int = 30,
                 heartbeat_interval: float = 1, transport=None,
                 wake_reviews: Callable | None = None, control_key: str | None = None):
        self.sessions = sessions
        base = url.rstrip('/')
        self.url = base + ('/embeddings' if base.endswith('/v1') else '/v1/embeddings')
        self.control_url = base.removesuffix('/v1') + '/internal/assistant/'
        self.control_key = control_key
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.transport = transport
        self.wake_reviews = wake_reviews
        self.jobs: set[asyncio.Task] = set()

    def _transaction(self, function, *args, **kwargs):
        with self.sessions() as db:
            value = function(db, *args, **kwargs)
            db.commit()
            return value

    async def embed(self, texts: list[str]) -> dict:
        # AnythingLLM must split its input before calling this endpoint. A single
        # tensor operation cannot safely be interrupted halfway through execution.
        if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip() or len(texts[0]) > 16000:
            raise ValueError('assistant embedding requires one nonempty text of at most 16000 characters')
        job = asyncio.create_task(self._run(texts))
        self.jobs.add(job)
        job.add_done_callback(self._finished)
        # A disconnected browser must not cancel the upstream HTTP request and
        # accidentally declare the GPU idle while the sync torch handler runs.
        return await asyncio.shield(job)

    def _finished(self, job):
        self.jobs.discard(job)
        if not job.cancelled():
            job.exception()  # Retrieve errors even if the HTTP caller disconnected.

    async def recover_once(self):
        if not self.control_key:
            return False
        if await asyncio.to_thread(self._transaction, gate.recover_unsubmitted,
                operation='embedding', lease_seconds=self.lease_seconds):
            if self.wake_reviews is not None:
                await asyncio.to_thread(self.wake_reviews)
            return True
        candidate = await asyncio.to_thread(self._transaction, gate.recovery_candidate,
            operation='embedding', lease_seconds=self.lease_seconds)
        if candidate is None:
            return False
        token, control_token, epoch = candidate
        path = self.control_url + control_token
        try:
            async with httpx.AsyncClient(headers={'X-CCheck-Control-Key': self.control_key},
                    timeout=15, transport=self.transport, trust_env=False, follow_redirects=False) as client:
                response = await client.post(path + '/cancel', params={'epoch': epoch})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get('epoch') != epoch or result.get('idle') is not True:
                    return False
                released = await asyncio.to_thread(self._transaction, gate.acknowledge_idle, token)
                if released and self.wake_reviews is not None:
                    await asyncio.to_thread(self.wake_reviews)
                try:
                    await client.delete(path, params={'epoch': epoch})
                except httpx.HTTPError:
                    logging.getLogger(__name__).warning('Recovered embedding reservation cleanup deferred')
                return released
        except (httpx.HTTPError, ValueError):
            return False

    async def _run(self, texts):
        token = await asyncio.to_thread(self._transaction, gate.claim, 'embedding',
                                        lease_seconds=self.lease_seconds)
        revoked = asyncio.Event()
        stop = asyncio.Event()
        idle_confirmed = True  # No request has yet been submitted.
        control_path, epoch = None, None

        async def monitor():
            while not stop.is_set():
                try:
                    allowed = await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                                       lease_seconds=self.lease_seconds)
                except Exception:
                    revoked.set()
                    return
                if not allowed:
                    revoked.set()
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_interval)
                except TimeoutError:
                    pass

        watcher = asyncio.create_task(monitor())
        try:
            allowed = await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                               lease_seconds=self.lease_seconds)
            if not allowed or revoked.is_set():
                raise AssistantInterrupted('review priority: embedding interrupted')
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                                         follow_redirects=False, trust_env=False) as client:
                target, params = self.url, None
                headers = {'Authorization': f'Bearer {self.api_key}'}
                control_path = None
                if self.control_key:
                    control_token = secrets.token_hex(32)
                    control_path = self.control_url + control_token
                    headers['X-CCheck-Control-Key'] = self.control_key
                    await asyncio.to_thread(self._transaction, gate.checkpoint_upstream, token, control_token,
                                            operation='embedding')
                    reserved = await client.post(control_path + '/reserve', headers=headers)
                    reserved.raise_for_status()
                    epoch = reserved.json()['epoch']
                    await asyncio.to_thread(self._transaction, gate.checkpoint_upstream, token, control_token, epoch,
                                            operation='embedding')
                    target, params = control_path + '/embeddings', {'epoch': epoch}
                    if not await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                                   lease_seconds=self.lease_seconds):
                        raise AssistantInterrupted('review priority: embedding interrupted')
                idle_confirmed = False
                try:
                    response = await client.post(target, params=params, headers=headers,
                        json={'model': self.model, 'input': texts, 'encoding_format': 'float'})
                finally:
                    if control_path:
                        ack = await client.post(control_path + '/cancel', params=params, headers=headers)
                        ack.raise_for_status()
                        proof = ack.json()
                        if not isinstance(proof, dict) or proof.get('epoch') != epoch or proof.get('idle') is not True:
                            raise httpx.ReadTimeout('embedding completion unconfirmed')
                        idle_confirmed = True
                # The pinned Jina sync handler returns only after vectors are on
                # the CPU. A timeout/transport failure does NOT provide this proof.
                idle_confirmed = True
                response.raise_for_status()
                result = response.json()
            allowed = await asyncio.to_thread(self._transaction, gate.heartbeat, token,
                                               lease_seconds=self.lease_seconds)
            if not allowed or revoked.is_set():
                raise AssistantInterrupted('review priority: embedding interrupted')
            return result
        finally:
            stop.set()
            await watcher
            if idle_confirmed:
                await asyncio.to_thread(self._transaction, gate.acknowledge_idle, token)
                if self.wake_reviews is not None:
                    try:
                        await asyncio.to_thread(self.wake_reviews)
                    except Exception:
                        logging.getLogger(__name__).exception('Failed to wake reviews after assistant drain')
                if control_path and epoch:
                    try:
                        async with httpx.AsyncClient(timeout=15, transport=self.transport,
                                trust_env=False, follow_redirects=False) as cleanup:
                            credentials = {'X-CCheck-Control-Key': self.control_key}
                            ack = await cleanup.post(control_path + '/cancel', params={'epoch': epoch}, headers=credentials)
                            ack.raise_for_status()
                            proof = ack.json()
                            if proof.get('epoch') == epoch and proof.get('idle') is True:
                                await cleanup.delete(control_path, params={'epoch': epoch}, headers=credentials)
                    except (httpx.HTTPError, ValueError, AttributeError):
                        logging.getLogger(__name__).warning('Embedding reservation cleanup deferred')
            else:
                await asyncio.to_thread(self._transaction, gate.fail_closed, token,
                                       'embedding upstream completion unconfirmed')

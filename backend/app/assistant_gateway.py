"""Internal model gateway. Run separately; never publish its port to the host."""
from contextlib import asynccontextmanager
import asyncio
import logging
import secrets
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.core.config import get_settings
from app.services import assistant_gate
from app.services.assistant_embedding import AssistantInterrupted, EmbeddingGateway
from app.services.assistant_chat import ChatGateway, ChatStreamError, collect_completion


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.session import SessionLocal
    from app.services.review_queue import dispatch_next_review
    settings = get_settings()
    key = settings.assistant_gateway_key
    if not settings.assistant_enabled or key is None or len(key.get_secret_value()) < 32:
        raise RuntimeError('assistant gateway requires enablement and a dedicated key of at least 32 characters')
    if not settings.rag_embedding_base_url or settings.rag_embedding_model != 'jina-code-embeddings-1.5b':
        raise RuntimeError('assistant gateway requires the existing Jina embedding endpoint')
    if not settings.assistant_qwen_base_url or not settings.assistant_qwen_api_key or not settings.assistant_control_key:
        raise RuntimeError('assistant gateway requires Qwen endpoint and separate control credentials')
    if len(settings.assistant_control_key.get_secret_value()) < 32:
        raise RuntimeError('assistant control key must have at least 32 characters')

    def wake():
        with SessionLocal() as db:
            dispatch_next_review(db)

    app.state.embedding = EmbeddingGateway(SessionLocal,
        url=settings.rag_embedding_base_url, api_key=settings.rag_embedding_api_key or '',
        model=settings.rag_embedding_model, timeout=settings.rag_embedding_timeout_seconds,
        lease_seconds=settings.assistant_lease_seconds, wake_reviews=wake,
        control_key=settings.assistant_control_key.get_secret_value())
    app.state.chat = ChatGateway(SessionLocal, url=settings.assistant_qwen_base_url,
        api_key=settings.assistant_qwen_api_key.get_secret_value(),
        control_key=settings.assistant_control_key.get_secret_value(),
        lease_seconds=settings.assistant_lease_seconds, wake_reviews=wake)
    stopping = asyncio.Event()

    async def recover_stale_chats():
        while not stopping.is_set():
            try:
                await app.state.chat.recover_once()
                await app.state.embedding.recover_once()
            except Exception:
                # Recovery failure never clears the durable gate or exposes secrets.
                logging.getLogger(__name__).warning('Assistant recovery probe failed; compute remains fenced')
            try:
                await asyncio.wait_for(stopping.wait(), timeout=5)
            except TimeoutError:
                pass

    recovery = asyncio.create_task(recover_stale_chats())
    try:
        yield
    finally:
        stopping.set()
        await recovery
        # Keep tracking active sync upstream operations during a graceful shutdown.
        # A hard termination leaves their durable leases fenced for recovery.
        jobs = list(app.state.embedding.jobs | app.state.chat.jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)


app = FastAPI(title='C-Check internal assistant gateway', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/health')
def health():
    return {'status': 'ok'}


def authenticate(authorization: Annotated[str | None, Header()] = None):
    key = get_settings().assistant_gateway_key
    expected = key.get_secret_value() if key else ''
    supplied = authorization.removeprefix('Bearer ') if authorization and authorization.startswith('Bearer ') else ''
    if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail='invalid gateway credentials')
    settings = get_settings()
    if not settings.assistant_enabled:
        raise HTTPException(status_code=503, detail='assistant disabled')
    if settings.deployment_maintenance_file is not None and settings.deployment_maintenance_file.exists():
        raise HTTPException(status_code=503, detail='deployment maintenance')


class EmbeddingRequest(BaseModel):
    model: Literal['jina-code-embeddings-1.5b']
    input: list[Annotated[str, StringConstraints(min_length=1, max_length=16000)]] = Field(min_length=1, max_length=1)
    encoding_format: Literal['float'] = 'float'


class ChatMessage(BaseModel):
    role: Literal['system', 'user', 'assistant']
    content: str = Field(max_length=32000)


class ChatRequest(BaseModel):
    model: Literal['qwen2.5-coder-32b-instruct-fp8']
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    stream: bool = False
    max_tokens: int = Field(default=1024, ge=1, le=2048)
    temperature: float = Field(default=.3, ge=0, le=2)
    stream_options: dict[Literal['include_usage'], bool] | None = None

    @model_validator(mode='after')
    def bounded_history(self):
        if sum(len(message.content) for message in self.messages) > 32000:
            raise ValueError('chat history exceeds input limit')
        return self


@app.post('/v1/chat/completions', dependencies=[Depends(authenticate)])
async def chat(body: ChatRequest):
    try:
        stream = await app.state.chat.start(body.model_dump(exclude_none=True))
    except assistant_gate.AssistantUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from None
    if not body.stream:
        try:
            return await collect_completion(stream, body.model)
        except ChatStreamError as exc:
            status_code = 409 if str(exc) == 'review_priority_interrupted' else 503
            raise HTTPException(status_code=status_code, detail=str(exc)) from None
    return StreamingResponse(stream, media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@app.post('/v1/embeddings', dependencies=[Depends(authenticate)])
async def embeddings(body: EmbeddingRequest):
    try:
        return await app.state.embedding.embed(body.input)
    except assistant_gate.AssistantUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from None
    except AssistantInterrupted:
        raise HTTPException(status_code=409, detail='review_priority_interrupted') from None
    except ValueError:
        raise HTTPException(status_code=422, detail='invalid embedding input') from None
    except httpx.HTTPError:
        # Do not leak upstream URLs, credentials, or response bodies.
        raise HTTPException(status_code=503, detail='embedding upstream unavailable') from None

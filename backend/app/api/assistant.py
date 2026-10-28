from typing import Annotated
from datetime import datetime
from uuid import UUID
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import User, AssistantConversation
from app.db.session import get_db
from app.services.assistant_gate import status
from app.services.anythingllm import AnythingLLM, AnythingLLMError


router = APIRouter(prefix="/assistant", tags=["assistant"])


def assistant_client() -> AnythingLLM:
    settings = get_settings()
    if not settings.assistant_enabled:
        raise HTTPException(503, 'assistant disabled')
    if not settings.assistant_anythingllm_url or not settings.assistant_anythingllm_api_key:
        raise HTTPException(503, 'assistant service not configured')
    return AnythingLLM(settings.assistant_anythingllm_url,
                       settings.assistant_anythingllm_api_key.get_secret_value())


def owned_conversation(db, conversation_id, user):
    conversation = db.scalar(select(AssistantConversation).where(
        AssistantConversation.id == str(conversation_id), AssistantConversation.owner_id == user.id))
    if conversation is None:
        raise HTTPException(404, 'conversation not found')
    return conversation


class ConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    rag_enabled: bool = True


class ConversationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    rag_enabled: bool
    status: str
    created_at: datetime
    updated_at: datetime


class AssistantMessage(BaseModel):
    message: str = Field(min_length=1, max_length=16000)


@router.post('/conversations/{conversation_id}/messages')
def send_message(
    conversation_id: UUID,
    body: AssistantMessage,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    conversation = owned_conversation(db, conversation_id, current_user)
    if conversation.status != 'ready':
        raise HTTPException(409, 'conversation not ready')
    if not body.message.strip():
        raise HTTPException(422, 'message cannot be blank')
    settings = get_settings()
    if settings.deployment_maintenance_file is not None and settings.deployment_maintenance_file.exists():
        raise HTTPException(503, 'deployment maintenance')
    availability = status(db, lease_seconds=settings.assistant_lease_seconds)
    if not availability['available']:
        raise HTTPException(409, availability['reason'])
    owner_id, identifier, rag_enabled = current_user.id, conversation.id, conversation.rag_enabled
    db.rollback()  # Do not hold status-read locks throughout a streaming response.

    async def events():
        async for event in client.stream_chat(owner_id, identifier, rag_enabled, body.message):
            yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@router.get('/conversations', response_model=list[ConversationView])
def conversations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return list(db.scalars(select(AssistantConversation).where(AssistantConversation.owner_id == current_user.id)
        .order_by(AssistantConversation.created_at.desc(), AssistantConversation.id.desc()).limit(limit).offset(offset)))


@router.post('/conversations', response_model=ConversationView, status_code=201)
def create_conversation(
    body: ConversationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    if not body.title.strip():
        raise HTTPException(422, 'conversation title cannot be blank')
    conversation = AssistantConversation(owner_id=current_user.id, title=body.title.strip(), rag_enabled=body.rag_enabled)
    db.add(conversation)
    db.commit()
    return provision_conversation(UUID(conversation.id), current_user, db, client)


@router.post('/conversations/{conversation_id}/prepare', response_model=ConversationView)
def provision_conversation(
    conversation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    conversation = owned_conversation(db, conversation_id, current_user)
    # Serialize workspace creation for the same user, across API processes.
    db.execute(select(User.id).where(User.id == current_user.id).with_for_update())
    try:
        client.ensure_thread(current_user.id, conversation.id, conversation.title, conversation.rag_enabled)
        conversation.status = 'ready'
        db.commit()
    except AnythingLLMError:
        db.rollback()
        conversation.status = 'error'
        db.commit()
        raise HTTPException(503, {'code': 'assistant_service_unavailable', 'conversation_id': conversation.id}) from None
    return conversation


@router.get('/conversations/{conversation_id}/messages')
def conversation_messages(
    conversation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    conversation = owned_conversation(db, conversation_id, current_user)
    try:
        return {'messages': client.history(current_user.id, conversation.id, conversation.rag_enabled)}
    except AnythingLLMError:
        raise HTTPException(503, 'assistant history unavailable') from None


@router.delete('/conversations/{conversation_id}', status_code=204)
def delete_conversation(
    conversation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    conversation = owned_conversation(db, conversation_id, current_user)
    try:
        client.delete_thread(current_user.id, conversation.id, conversation.rag_enabled)
    except AnythingLLMError:
        raise HTTPException(503, 'assistant conversation deletion incomplete') from None
    db.delete(conversation)
    db.commit()
    return Response(status_code=204)


class AssistantStatus(BaseModel):
    enabled: bool
    available: bool
    reason: str
    cancel_requested: bool = False
    operation: str | None = None


@router.get("/status", response_model=AssistantStatus)
def assistant_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AssistantStatus:
    settings = get_settings()
    if not settings.assistant_enabled:
        return AssistantStatus(enabled=False, available=False, reason="disabled")
    if settings.deployment_maintenance_file is not None and settings.deployment_maintenance_file.exists():
        return AssistantStatus(enabled=True, available=False, reason="maintenance")
    return AssistantStatus(enabled=True, **status(db, lease_seconds=settings.assistant_lease_seconds))

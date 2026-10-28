import hashlib
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.assistant import assistant_client
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import AssistantDocument, User
from app.db.session import get_db
from app.services.anythingllm import AnythingLLM
from app.services.assistant_gate import status
from app.services.assistant_documents import (
    ALLOWED_EXTENSIONS, MAX_BATCH_BYTES, MAX_BATCH_FILES, MAX_FILE_BYTES, document_path, enqueue_document,
)


router = APIRouter(prefix='/assistant/documents', tags=['assistant'])


def require_document_admission(db):
    settings = get_settings()
    if settings.deployment_maintenance_file is not None and settings.deployment_maintenance_file.exists():
        raise HTTPException(503, 'deployment maintenance')
    availability = status(db, lease_seconds=settings.assistant_lease_seconds)
    if not availability['available']:
        raise HTTPException(409, availability['reason'])


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    size_bytes: int
    status: str
    error: str | None
    created_at: datetime
    updated_at: datetime


def dispatch(db, document):
    try:
        enqueue_document(document.id)
    except Exception:
        document.status = 'error'
        document.error = 'document_queue_unavailable'
        db.commit()


@router.get('', response_model=list[DocumentView])
def documents(current_user: Annotated[User, Depends(get_current_user)],
              db: Annotated[Session, Depends(get_db)]):
    return list(db.scalars(select(AssistantDocument).where(AssistantDocument.owner_id == current_user.id)
                          .order_by(AssistantDocument.created_at.desc(), AssistantDocument.id.desc())))


@router.post('', response_model=list[DocumentView], status_code=202)
def upload_documents(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
    files: Annotated[list[UploadFile], File()],
):
    if not 1 <= len(files) <= MAX_BATCH_FILES:
        raise HTTPException(422, 'upload between 1 and 20 files')
    names = [Path((file.filename or '').replace('\\', '/')).name for file in files]
    if any(not name or len(name) > 255 or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS for name in names):
        raise HTTPException(422, 'unsupported document filename or format')
    require_document_admission(db)
    db.rollback()
    written, records, total = [], [], 0
    try:
        for upload, filename in zip(files, names):
            identifier = str(uuid4())
            path = document_path(current_user.id, identifier)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            size, digest = 0, hashlib.sha256()
            with path.open('xb') as destination:
                written.append(path)
                path.chmod(0o600)
                while chunk := upload.file.read(64 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
                        raise HTTPException(413, 'document exceeds upload size limit')
                    digest.update(chunk)
                    destination.write(chunk)
            if size == 0:
                raise HTTPException(422, 'empty document')
            document = AssistantDocument(id=identifier, owner_id=current_user.id, filename=filename,
                size_bytes=size, sha256=digest.hexdigest(), status='queued', locations=[])
            db.add(document)
            records.append(document)
        db.commit()
    except BaseException:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for document in records:
        dispatch(db, document)
    return records


@router.post('/{document_id}/retry', response_model=DocumentView, status_code=202)
def retry_document(
    document_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[AnythingLLM, Depends(assistant_client)],
):
    document = db.scalar(select(AssistantDocument).where(
        AssistantDocument.id == str(document_id), AssistantDocument.owner_id == current_user.id))
    if document is None:
        raise HTTPException(404, 'document not found')
    require_document_admission(db)
    claimed = db.execute(update(AssistantDocument).where(AssistantDocument.id == document.id,
        AssistantDocument.status.in_(['paused', 'error'])).values(status='queued', error=None))
    db.commit()
    if claimed.rowcount != 1:
        raise HTTPException(409, 'document is not retryable')
    db.refresh(document)
    dispatch(db, document)
    return document

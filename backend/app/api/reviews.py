from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.db.models import ReviewFile, ReviewTask, User
from app.db.session import get_db
from app.schemas.reviews import ReviewTaskResponse, ReviewTaskSummaryResponse, TextReviewRequest
from app.services.submissions import (
    Submission,
    SubmissionError,
    collect_archive_submission,
    collect_file_submission,
    collect_text_submission,
    create_review_task,
)


router = APIRouter(prefix="/reviews", tags=["reviews"])


def _unprocessable(exc: SubmissionError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _create_task(
    db: Session,
    current_user: User,
    model_node_id: str,
    submission: Submission,
) -> ReviewTask:
    try:
        return create_review_task(
            db,
            owner=current_user,
            model_node_id=model_node_id,
            submission=submission,
        )
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc


def _visible_task_query(current_user: User):
    return select(ReviewTask).where(ReviewTask.owner_id == current_user.id)


@router.post("/text", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
def submit_text(
    request: TextReviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewTask:
    try:
        submission = collect_text_submission(request.source_text, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    return _create_task(db, current_user, request.model_node_id, submission)


@router.post("/file", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_file(
    model_node_id: Annotated[str, Form(min_length=1, max_length=36)],
    file: Annotated[UploadFile, File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewTask:
    try:
        content = await file.read(settings.upload_max_file_bytes + 1)
        submission = collect_file_submission(file.filename or "", content, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    return _create_task(db, current_user, model_node_id, submission)


@router.post("/archive", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_archive(
    model_node_id: Annotated[str, Form(min_length=1, max_length=36)],
    file: Annotated[UploadFile, File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewTask:
    try:
        content = await file.read(settings.upload_max_archive_bytes + 1)
        if len(content) > settings.upload_max_archive_bytes:
            raise SubmissionError("zip archive exceeds upload size limit")
        submission = collect_archive_submission(file.filename or "", content, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    return _create_task(db, current_user, model_node_id, submission)


@router.get("", response_model=list[ReviewTaskSummaryResponse])
def list_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ReviewTask]:
    query = (
        _visible_task_query(current_user)
        .order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(query).all())


@router.get("/{task_id}", response_model=ReviewTaskResponse)
def get_review(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ReviewTask:
    query = _visible_task_query(current_user).options(
        selectinload(ReviewTask.files).load_only(
            ReviewFile.id,
            ReviewFile.relative_path,
            ReviewFile.size_bytes,
        )
    )
    task = db.scalar(query.where(ReviewTask.id == task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    task = db.scalar(_visible_task_query(current_user).where(ReviewTask.id == task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review task not found")
    db.delete(task)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

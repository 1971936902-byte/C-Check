from typing import Annotated
import json

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.db.models import ModelNode, Report, ReviewFile, ReviewTask, TaskStatus, User
from app.db.session import get_db
from app.schemas.reviews import ReviewTaskPageResponse, ReviewTaskResponse, TextReviewRequest
from app.services.submissions import (
    Submission,
    SubmissionError,
    collect_archive_submission,
    collect_file_submission,
    collect_folder_submission,
    collect_text_submission,
    create_review_task,
)
from app.services.review_queue import attach_queue_positions, dispatch_next_review, pin_queued_task


router = APIRouter(prefix="/reviews", tags=["reviews"])
SEVERITY_FILTERS = {"high", "medium", "low", "suggestion"}


def _unprocessable(exc: SubmissionError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _create_task(
    db: Session,
    current_user: User,
    model_node_id: str,
    submission: Submission,
    check_types: list[str],
    display_name: str | None = None,
) -> ReviewTask:
    try:
        return create_review_task(
            db,
            owner=current_user,
            model_node_id=model_node_id,
            submission=submission,
            check_types=check_types,
            display_name=display_name,
        )
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc


def _with_queue_position(db: Session, task: ReviewTask) -> ReviewTask:
    attach_queue_positions(db, [task])
    return task


def _visible_task_query(current_user: User):
    query = select(ReviewTask).options(selectinload(ReviewTask.owner))
    if current_user.role != "admin":
        query = query.where(ReviewTask.owner_id == current_user.id)
    return query


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
    task = _create_task(db, current_user, request.model_node_id, submission, request.check_types, request.display_name)
    return _with_queue_position(db, task)


def _parse_check_types(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SubmissionError("check_types must be a JSON array") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise SubmissionError("check_types must be a JSON array of strings")
    return parsed


@router.post("/file", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_file(
    model_node_id: Annotated[str, Form(min_length=1, max_length=36)],
    check_types: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    display_name: Annotated[str | None, Form(max_length=128)] = None,
) -> ReviewTask:
    try:
        content = await file.read(settings.upload_max_file_bytes + 1)
        submission = collect_file_submission(file.filename or "", content, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    task = _create_task(db, current_user, model_node_id, submission, _parse_check_types(check_types), display_name)
    return _with_queue_position(db, task)


@router.post("/archive", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_archive(
    model_node_id: Annotated[str, Form(min_length=1, max_length=36)],
    check_types: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    display_name: Annotated[str | None, Form(max_length=128)] = None,
) -> ReviewTask:
    try:
        content = await file.read(settings.upload_max_archive_bytes + 1)
        if len(content) > settings.upload_max_archive_bytes:
            raise SubmissionError("zip archive exceeds upload size limit")
        submission = collect_archive_submission(file.filename or "", content, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    task = _create_task(db, current_user, model_node_id, submission, _parse_check_types(check_types), display_name)
    return _with_queue_position(db, task)


@router.post("/folder", response_model=ReviewTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_folder(
    model_node_id: Annotated[str, Form(min_length=1, max_length=36)],
    check_types: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    display_name: Annotated[str | None, Form(max_length=128)] = None,
) -> ReviewTask:
    try:
        submitted = []
        for file in files:
            content = await file.read(settings.upload_max_file_bytes + 1)
            submitted.append((file.filename or "", content))
        submission = collect_folder_submission(submitted, settings)
    except SubmissionError as exc:
        raise _unprocessable(exc) from exc
    task = _create_task(db, current_user, model_node_id, submission, _parse_check_types(check_types), display_name)
    return _with_queue_position(db, task)


@router.get("", response_model=ReviewTaskPageResponse)
def list_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    tester_name: str | None = None,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    model_node_id: str | None = None,
    severity: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    sort_by: Annotated[str, Query(pattern="^(display_name|tester_name|model|status|file_count|finding_count|duration_ms|created_at)$")] = "created_at",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> ReviewTaskPageResponse:
    query = _visible_task_query(current_user)
    if keyword:
        query = query.where(ReviewTask.display_name.contains(keyword))
    if tester_name:
        query = query.where(ReviewTask.owner.has(User.username.contains(tester_name)))
    if task_status is not None:
        query = query.where(ReviewTask.status == task_status)
    if model_node_id:
        query = query.where(ReviewTask.model_node_id == model_node_id)
    if start_time:
        query = query.where(ReviewTask.created_at >= start_time)
    if end_time:
        query = query.where(ReviewTask.created_at <= end_time)
    if severity == "":
        severity = None
    if severity and severity not in SEVERITY_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="severity must be one of high, medium, low, suggestion",
        )
    if severity:
        query = query.join(ReviewTask.report).where(getattr(Report, f"{severity}_count") > 0)
    sort_columns = {
        "display_name": ReviewTask.display_name,
        "status": ReviewTask.status,
        "file_count": ReviewTask.file_count,
        "finding_count": ReviewTask.finding_count,
        "duration_ms": ReviewTask.duration_ms,
        "created_at": ReviewTask.created_at,
    }
    if sort_by == "tester_name":
        query = query.join(ReviewTask.owner)
        sort_column = User.username
    elif sort_by == "model":
        query = query.join(ReviewTask.model_node)
        sort_column = ModelNode.display_name
    else:
        sort_column = sort_columns[sort_by]
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    items = list(db.scalars(query.order_by(order, ReviewTask.id.desc()).offset(offset).limit(limit)).all())
    attach_queue_positions(db, items)
    return ReviewTaskPageResponse(items=items, total=total)


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
    attach_queue_positions(db, [task])
    return task


@router.post("/{task_id}/pin", response_model=ReviewTaskResponse)
def pin_review(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ReviewTask:
    task = db.scalar(_visible_task_query(current_user).where(ReviewTask.id == task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review task not found")
    try:
        return pin_queued_task(db, task)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
    dispatch_next_review(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

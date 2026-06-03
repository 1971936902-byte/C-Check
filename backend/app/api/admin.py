from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.security import hash_password
from app.db.models import ModelNode, PromptVersion, ReviewTask, TaskStatus, User
from app.db.session import get_db
from app.schemas.admin import (
    AdminModelNodeResponse,
    AdminTaskResponse,
    AdminUserResponse,
    DashboardResponse,
    ModelEnabledRequest,
    ModelNodeRequest,
    PasswordResetRequest,
    PromptCreateRequest,
    PromptResponse,
    PromptUpdateRequest,
    UserCreateRequest,
    UserEnabledRequest,
)
from app.services.prompts import activate_prompt, create_prompt_version


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{kind} not found")


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(db: Annotated[Session, Depends(get_db)]) -> DashboardResponse:
    statuses = dict(
        db.execute(select(ReviewTask.status, func.count()).group_by(ReviewTask.status)).all()
    )
    return DashboardResponse(
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        enabled_users=db.scalar(select(func.count()).select_from(User).where(User.is_enabled.is_(True))) or 0,
        models=db.scalar(select(func.count()).select_from(ModelNode)) or 0,
        enabled_models=db.scalar(
            select(func.count()).select_from(ModelNode).where(ModelNode.is_enabled.is_(True))
        )
        or 0,
        tasks=sum(statuses.values()),
        queued_tasks=statuses.get(TaskStatus.QUEUED, 0),
        running_tasks=statuses.get(TaskStatus.RUNNING, 0),
        completed_tasks=statuses.get(TaskStatus.COMPLETED, 0),
        failed_tasks=statuses.get(TaskStatus.FAILED, 0),
    )


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(db: Annotated[Session, Depends(get_db)]) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at.desc())).all())


@router.post("/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED)
def create_user(request: UserCreateRequest, db: Annotated[Session, Depends(get_db)]) -> User:
    user = User(username=request.username, password_hash=hash_password(request.password), role=request.role)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already exists") from exc
    db.refresh(user)
    return user


@router.patch("/users/{user_id}/enabled", response_model=AdminUserResponse)
def set_user_enabled(
    user_id: str, request: UserEnabledRequest, db: Annotated[Session, Depends(get_db)]
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise _not_found("user")
    user.is_enabled = request.is_enabled
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_password(
    user_id: str, request: PasswordResetRequest, db: Annotated[Session, Depends(get_db)]
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise _not_found("user")
    user.password_hash = hash_password(request.password)
    user.token_version += 1
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/models", response_model=list[AdminModelNodeResponse])
def list_models(db: Annotated[Session, Depends(get_db)]) -> list[ModelNode]:
    return list(db.scalars(select(ModelNode).order_by(ModelNode.created_at.desc())).all())


@router.post("/models", response_model=AdminModelNodeResponse, status_code=status.HTTP_201_CREATED)
def create_model(request: ModelNodeRequest, db: Annotated[Session, Depends(get_db)]) -> ModelNode:
    node = ModelNode(**request.model_dump())
    if db.scalar(select(func.count()).select_from(ModelNode).where(ModelNode.is_default.is_(True))) == 0:
        node.is_default = True
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.put("/models/{model_id}", response_model=AdminModelNodeResponse)
def update_model(
    model_id: str, request: ModelNodeRequest, db: Annotated[Session, Depends(get_db)]
) -> ModelNode:
    node = db.get(ModelNode, model_id)
    if node is None:
        raise _not_found("model node")
    for field, value in request.model_dump().items():
        if field == "api_key" and field not in request.model_fields_set:
            continue
        setattr(node, field, value)
    db.commit()
    db.refresh(node)
    return node


@router.patch("/models/{model_id}/enabled", response_model=AdminModelNodeResponse)
def set_model_enabled(
    model_id: str, request: ModelEnabledRequest, db: Annotated[Session, Depends(get_db)]
) -> ModelNode:
    node = db.get(ModelNode, model_id)
    if node is None:
        raise _not_found("model node")
    node.is_enabled = request.is_enabled
    db.commit()
    db.refresh(node)
    return node


@router.post("/models/{model_id}/default", response_model=AdminModelNodeResponse)
def set_default_model(model_id: str, db: Annotated[Session, Depends(get_db)]) -> ModelNode:
    node = db.get(ModelNode, model_id)
    if node is None:
        raise _not_found("model node")
    if not node.is_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="disabled model node cannot be the default")
    db.execute(update(ModelNode).values(is_default=False))
    node.is_default = True
    db.commit()
    db.refresh(node)
    return node


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: str, db: Annotated[Session, Depends(get_db)]) -> Response:
    node = db.get(ModelNode, model_id)
    if node is None:
        raise _not_found("model node")
    db.delete(node)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="model node is referenced by review tasks; disable it instead",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/prompts", response_model=list[PromptResponse])
def list_prompts(db: Annotated[Session, Depends(get_db)]) -> list[PromptVersion]:
    return list(db.scalars(select(PromptVersion).order_by(PromptVersion.version.asc())).all())


@router.post("/prompts", response_model=PromptResponse, status_code=status.HTTP_201_CREATED)
def create_prompt(
    request: PromptCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> PromptVersion:
    return create_prompt_version(db, body=request.body, creator=admin)


@router.post("/prompts/{prompt_id}/activate", response_model=PromptResponse)
def set_active_prompt(prompt_id: str, db: Annotated[Session, Depends(get_db)]) -> PromptVersion:
    prompt = db.get(PromptVersion, prompt_id)
    if prompt is None:
        raise _not_found("prompt")
    return activate_prompt(db, prompt)


@router.put("/prompts/{prompt_id}", response_model=PromptResponse)
def update_prompt(
    prompt_id: str, request: PromptUpdateRequest, db: Annotated[Session, Depends(get_db)]
) -> PromptVersion:
    prompt = db.get(PromptVersion, prompt_id)
    if prompt is None:
        raise _not_found("prompt")
    prompt.body = request.body
    db.commit()
    db.refresh(prompt)
    return prompt


@router.delete("/prompts/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt(prompt_id: str, db: Annotated[Session, Depends(get_db)]) -> Response:
    prompt = db.get(PromptVersion, prompt_id)
    if prompt is None:
        raise _not_found("prompt")
    if prompt.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="active prompt cannot be deleted")
    if (db.scalar(select(func.count()).select_from(PromptVersion)) or 0) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="at least one prompt version is required")
    db.delete(prompt)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tasks", response_model=list[AdminTaskResponse])
def list_tasks(
    db: Annotated[Session, Depends(get_db)],
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    owner_id: str | None = None,
    model_node_id: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ReviewTask]:
    query = select(ReviewTask)
    if task_status is not None:
        query = query.where(ReviewTask.status == task_status)
    if owner_id:
        query = query.where(ReviewTask.owner_id == owner_id)
    if model_node_id:
        query = query.where(ReviewTask.model_node_id == model_node_id)
    if start_time:
        query = query.where(ReviewTask.created_at >= start_time)
    if end_time:
        query = query.where(ReviewTask.created_at <= end_time)
    return list(
        db.scalars(query.order_by(ReviewTask.created_at.desc()).offset(offset).limit(limit)).all()
    )

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.config import Settings, get_settings
from app.db.models import ModelNode, User
from app.db.session import get_db
from app.services.model_router import ModelInvocationError, check_model_health


router = APIRouter(prefix="/models", tags=["models"])
MOCK_MODEL_BASE_URL = "mock://local"


class ModelNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    model_identifier: str
    base_url: str
    timeout_seconds: int
    is_enabled: bool
    is_default: bool
    description: str | None


def ensure_mock_model_node(db: Session, settings: Settings) -> None:
    if not settings.mock_model_enabled:
        return
    node = db.scalar(select(ModelNode).where(ModelNode.base_url == MOCK_MODEL_BASE_URL))
    if node is None:
        db.add(
            ModelNode(
                display_name="Mock 模式",
                model_identifier="mock-reviewer",
                base_url=MOCK_MODEL_BASE_URL,
                timeout_seconds=30,
                is_enabled=True,
                is_default=False,
                description="模拟审查模式，用于快速验证任务流程，不调用真实模型。",
            )
        )
        db.commit()
        return
    changed = False
    if not node.is_enabled:
        node.is_enabled = True
        changed = True
    if node.display_name != "Mock 模式":
        node.display_name = "Mock 模式"
        changed = True
    if changed:
        db.commit()


@router.get("", response_model=list[ModelNodeResponse])
def list_enabled_models(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ModelNode]:
    ensure_mock_model_node(db, settings)
    query = select(ModelNode).where(ModelNode.is_enabled.is_(True)).order_by(ModelNode.is_default.desc(), ModelNode.created_at.asc())
    if current_user.role != "admin":
        query = query.where(ModelNode.is_default.is_(True))
    return list(db.scalars(query).all())


@router.post("/{model_id}/health")
async def model_health(
    model_id: str,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    node = db.get(ModelNode, model_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model node not found")
    try:
        return await check_model_health(node)
    except ModelInvocationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

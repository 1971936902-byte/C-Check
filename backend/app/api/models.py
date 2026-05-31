from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.models import ModelNode, User
from app.db.session import get_db
from app.services.model_router import ModelInvocationError, check_model_health


router = APIRouter(prefix="/models", tags=["models"])


class ModelNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    model_identifier: str
    base_url: str
    timeout_seconds: int
    is_enabled: bool
    description: str | None


@router.get("", response_model=list[ModelNodeResponse])
def list_enabled_models(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ModelNode]:
    return list(db.scalars(select(ModelNode).where(ModelNode.is_enabled.is_(True))).all())


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

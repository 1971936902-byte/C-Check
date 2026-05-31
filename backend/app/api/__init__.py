from fastapi import APIRouter

from app.api.auth import router as auth_router


router = APIRouter(prefix="/api")
router.include_router(auth_router)

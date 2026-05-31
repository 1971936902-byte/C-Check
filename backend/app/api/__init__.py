from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.models import router as models_router
from app.api.reports import router as reports_router
from app.api.reviews import router as reviews_router


router = APIRouter(prefix="/api")
router.include_router(admin_router)
router.include_router(auth_router)
router.include_router(models_router)
router.include_router(reports_router)
router.include_router(reviews_router)

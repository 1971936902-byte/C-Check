from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.core.bootstrap import ensure_initial_admin
from app.core.config import get_settings
from app.db import session as db_session
from app.services.prompts import get_active_prompt


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    with db_session.SessionLocal() as db:
        ensure_initial_admin(db, get_settings())
        get_active_prompt(db)
    yield


app = FastAPI(title="C-Check API", lifespan=lifespan)
app.include_router(router)

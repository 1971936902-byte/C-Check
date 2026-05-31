from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import User


def ensure_initial_admin(db: Session, settings: Settings) -> User:
    admin = db.scalar(select(User).where(User.role == "admin"))
    if admin is not None:
        return admin

    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    db.add(admin)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing_user = db.scalar(select(User).where(User.username == settings.admin_username))
        if existing_user is not None and existing_user.role == "admin":
            return existing_user
        if existing_user is None:
            raise RuntimeError("failed to initialize admin after database integrity error") from exc
        raise RuntimeError(
            f"reserved admin username {settings.admin_username!r} belongs to a non-admin user"
        ) from exc
    db.refresh(admin)
    return admin

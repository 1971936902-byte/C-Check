from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import User


@pytest.fixture(autouse=True)
def use_secure_test_jwt_secret(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-at-least-32-characters-long")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_password_hash_round_trip_and_rejects_wrong_password():
    from app.core.security import hash_password, verify_password

    password_hash = hash_password("correct horse battery staple")

    assert password_hash != "correct horse battery staple"
    assert verify_password("correct horse battery staple", password_hash) is True
    assert verify_password("wrong password", password_hash) is False


def test_jwt_round_trip_and_rejects_expired_token():
    from app.core.security import create_access_token, decode_access_token

    secret = "test-jwt-secret-at-least-32-characters-long"
    token = create_access_token("user-id", 3, secret, timedelta(minutes=5))

    assert decode_access_token(token, secret) == ("user-id", 3)

    expired_token = create_access_token("user-id", 3, secret, timedelta(seconds=-1))
    with pytest.raises(ValueError, match="invalid access token"):
        decode_access_token(expired_token, secret)


@pytest.mark.parametrize("omitted_claim", ["sub", "exp", "token_version"])
def test_jwt_rejects_token_without_required_claim(omitted_claim):
    from app.core.security import ALGORITHM, decode_access_token

    secret = "test-jwt-secret-at-least-32-characters-long"
    payload = {
        "sub": "user-id",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "token_version": 0,
    }
    payload.pop(omitted_claim)
    token = jwt.encode(payload, secret, algorithm=ALGORITHM)

    with pytest.raises(ValueError, match="invalid access token"):
        decode_access_token(token, secret)


def test_lifespan_initializes_single_admin_from_settings(db_session_factory):
    from app.main import app

    with TestClient(app):
        pass
    with TestClient(app):
        pass

    with db_session_factory() as db:
        admins = db.scalars(select(User).where(User.role == "admin")).all()

    assert len(admins) == 1
    assert admins[0].username == "admin"
    assert admins[0].password_hash != "change-this-password"


def test_login_me_and_password_change_flow():
    from app.main import app

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "change-this-password"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        me = client.get("/api/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json() == {
            "id": me.json()["id"],
            "username": "admin",
            "role": "admin",
            "is_enabled": True,
        }
        assert "password_hash" not in me.json()

        changed = client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current_password": "change-this-password",
                "new_password": "new-secure-password",
            },
        )
        assert changed.status_code == 204

        stale_me = client.get("/api/auth/me", headers=headers)
        assert stale_me.status_code == 401

        old_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "change-this-password"},
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "new-secure-password"},
        )
        assert new_login.status_code == 200


def test_password_change_rejects_wrong_current_password():
    from app.main import app

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "change-this-password"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        response = client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current_password": "wrong-password",
                "new_password": "new-secure-password",
            },
        )

    assert response.status_code == 401


def test_password_change_rejects_short_new_password():
    from app.main import app

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "change-this-password"},
        )
        response = client.post(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            json={
                "current_password": "change-this-password",
                "new_password": "too-short",
            },
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "password_change",
    [
        {"current_password": "x" * 1025, "new_password": "new-secure-password"},
        {"current_password": "change-this-password", "new_password": "x" * 1025},
    ],
)
def test_password_change_rejects_oversized_credentials(password_change):
    from app.main import app

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "change-this-password"},
        )
        response = client.post(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            json=password_change,
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/auth/login", {"username": "x" * 65, "password": "password"}),
        ("/api/auth/login", {"username": "admin", "password": "x" * 1025}),
    ],
)
def test_login_rejects_oversized_credentials(path, payload):
    from app.main import app

    with TestClient(app) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 422


def test_login_rejects_wrong_credentials():
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )

    assert response.status_code == 401


def test_disabled_user_is_forbidden_for_login_and_existing_token(db_session_factory):
    from app.core.security import hash_password
    from app.main import app

    with TestClient(app) as client:
        with db_session_factory() as db:
            user = User(
                username="disabled-reviewer",
                password_hash=hash_password("reviewer-password"),
                is_enabled=False,
            )
            db.add(user)
            db.commit()
            user_id = user.id

        login = client.post(
            "/api/auth/login",
            json={"username": "disabled-reviewer", "password": "reviewer-password"},
        )
        assert login.status_code == 403

        from app.core.security import create_access_token

        token = create_access_token(
            user_id,
            0,
            "test-jwt-secret-at-least-32-characters-long",
            timedelta(minutes=5),
        )
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 403


def test_me_rejects_token_for_unknown_user():
    from app.core.security import create_access_token
    from app.main import app

    token = create_access_token(
        "missing-user-id",
        0,
        "test-jwt-secret-at-least-32-characters-long",
        timedelta(minutes=5),
    )

    with TestClient(app) as client:
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_me_rejects_invalid_token():
    from app.main import app

    with TestClient(app) as client:
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer definitely-not-a-token"},
        )

    assert response.status_code == 401


def test_require_admin_rejects_regular_user():
    from app.api.deps import require_admin

    with pytest.raises(HTTPException) as exc_info:
        require_admin(User(username="reviewer", password_hash="hash", role="user"))

    assert exc_info.value.status_code == 403


def test_require_admin_accepts_admin():
    from app.api.deps import require_admin

    admin = User(username="admin", password_hash="hash", role="admin")

    assert require_admin(admin) is admin


def test_initial_admin_seed_accepts_concurrent_admin_insert():
    from app.core.bootstrap import ensure_initial_admin
    from app.core.config import Settings

    admin = User(username="admin", password_hash="hash", role="admin")
    db = Mock()
    db.scalar.side_effect = [None, admin]
    db.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate username"))

    assert ensure_initial_admin(db, Settings(_env_file=None)) is admin
    db.rollback.assert_called_once_with()


def test_initial_admin_seed_rejects_conflicting_non_admin_username(db_session):
    from app.core.bootstrap import ensure_initial_admin
    from app.core.config import Settings

    db_session.add(User(username="admin", password_hash="hash", role="user"))
    db_session.commit()

    with pytest.raises(RuntimeError, match="reserved admin username"):
        ensure_initial_admin(db_session, Settings(_env_file=None))

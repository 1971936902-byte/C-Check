from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash


ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_password: str) -> bool:
    return password_hash.verify(password, encoded_password)


def create_access_token(
    subject: str,
    token_version: int,
    secret: str,
    expires_delta: timedelta,
) -> str:
    expires_at = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(
        {"sub": subject, "exp": expires_at, "token_version": token_version},
        secret,
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str, secret: str) -> tuple[str, int]:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "token_version"]},
        )
        subject = payload["sub"]
        token_version = payload["token_version"]
    except (jwt.InvalidTokenError, KeyError, TypeError) as exc:
        raise ValueError("invalid access token") from exc
    if not isinstance(subject, str) or not subject:
        raise ValueError("invalid access token")
    if type(token_version) is not int or token_version < 0:
        raise ValueError("invalid access token")
    return subject, token_version

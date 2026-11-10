import uuid
from datetime import timedelta

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.errors import unauthorized
from app.core.types import utcnow


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=get_settings().BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def burn_password_check(password: str) -> None:
    """Keeps response time similar for unknown e-mail addresses."""
    verify_password(password, _DUMMY_HASH)


def _encode(claims: dict) -> str:
    s = get_settings()
    return jwt.encode(claims, s.SECRET_KEY, algorithm=s.JWT_ALGORITHM)


def create_access_token(user_id: int, session_id: str) -> tuple[str, int]:
    s = get_settings()
    expires = timedelta(minutes=s.ACCESS_TOKEN_EXPIRE_MINUTES)
    now = utcnow()
    token = _encode(
        {"sub": str(user_id), "sid": session_id, "type": "access", "iat": now, "exp": now + expires,
         "jti": uuid.uuid4().hex}
    )
    return token, int(expires.total_seconds())


def create_refresh_token(user_id: int, session_id: str, jti: str):
    s = get_settings()
    now = utcnow()
    expires_at = now + timedelta(days=s.REFRESH_TOKEN_EXPIRE_DAYS)
    token = _encode(
        {"sub": str(user_id), "sid": session_id, "type": "refresh", "iat": now, "exp": expires_at, "jti": jti}
    )
    return token, expires_at


def decode_token(token: str, expected_type: str) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.SECRET_KEY, algorithms=[s.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise unauthorized("TOKEN_EXPIRED", "The token has expired.")
    except jwt.PyJWTError:
        raise unauthorized("INVALID_TOKEN", "The token is invalid.")
    if payload.get("type") != expected_type:
        raise unauthorized("INVALID_TOKEN", "Wrong token type.")
    return payload

"""Password hashing, access tokens and CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from vitality_bss.config import get_settings

ALGORITHM = "HS256"
ISSUER = "vitality-bss"
_BCRYPT_MAX_BYTES = 72


class PasswordPolicyError(ValueError):
    pass


def _pw_bytes(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes; pre-hash longer inputs so they are not truncated.
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raw = hashlib.sha256(raw).hexdigest().encode("ascii")
    return raw


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt(rounds=get_settings().BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(password), hashed.encode("ascii"))
    except ValueError:
        return False


def check_password_policy(password: str, min_length: int | None = None) -> None:
    """Raise PasswordPolicyError unless the password is long and mixed enough."""
    min_len = min_length if min_length is not None else get_settings().MIN_PASSWORD_LENGTH
    if len(password) < min_len:
        raise PasswordPolicyError(f"Password must be at least {min_len} characters")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise PasswordPolicyError("Password must mix at least three of: lowercase, uppercase, digits, symbols")


@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    token_version: int


def create_access_token(user_id: int, token_version: int, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    minutes = expires_minutes if expires_minutes is not None else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    payload = {
        "sub": str(user_id),
        "ver": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        "iss": ISSUER,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> TokenClaims | None:
    try:
        payload = jwt.decode(
            token,
            get_settings().SECRET_KEY,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["sub", "exp", "iat"]},
        )
        return TokenClaims(user_id=int(payload["sub"]), token_version=int(payload.get("ver", 0)))
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


def csrf_token_for(session_token: str) -> str:
    """Deterministic CSRF token bound to the session token (synchronizer pattern)."""
    key = get_settings().SECRET_KEY.encode("utf-8")
    return hmac.new(key, b"csrf:" + session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session_token: str, candidate: str | None) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(csrf_token_for(session_token), candidate)

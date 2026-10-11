"""Request dependencies: authentication, roles and CSRF."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from vitality_bss.db import get_db
from vitality_bss.models import User
from vitality_bss.security import decode_access_token, verify_csrf

COOKIE_NAME = "vbss_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LoginRequired(Exception):
    """Raised by page dependencies; handled by redirecting to /login."""


@dataclass
class Auth:
    user: User
    token: str
    via_cookie: bool


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _extract_token(request: Request) -> tuple[str | None, bool]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None, False
    cookie = request.cookies.get(COOKIE_NAME)
    return (cookie or None), True


def resolve_auth(request: Request, db: Session) -> Auth | None:
    token, via_cookie = _extract_token(request)
    if not token:
        return None
    claims = decode_access_token(token)
    if claims is None:
        return None
    user = db.get(User, claims.user_id)
    if user is None or not user.is_active or user.token_version != claims.token_version:
        return None
    return Auth(user=user, token=token, via_cookie=via_cookie)


def api_auth(request: Request, db: Session = Depends(get_db)) -> Auth:
    auth = resolve_auth(request, db)
    if auth is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if (
        auth.via_cookie
        and request.method not in SAFE_METHODS
        and not verify_csrf(auth.token, request.headers.get(CSRF_HEADER))
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")
    return auth


def api_user(auth: Auth = Depends(api_auth)) -> User:
    return auth.user


def require_role(*roles: str) -> Callable[[User], User]:
    def _dep(user: User = Depends(api_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _dep


def page_auth(request: Request, db: Session = Depends(get_db)) -> Auth:
    auth = resolve_auth(request, db)
    if auth is None:
        raise LoginRequired()
    return auth


def check_form_csrf(auth: Auth, submitted: str | None) -> None:
    if auth.via_cookie and not verify_csrf(auth.token, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")


def page_role(auth: Auth, *roles: str) -> None:
    if auth.user.role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

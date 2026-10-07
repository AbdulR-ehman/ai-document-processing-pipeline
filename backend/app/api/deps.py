"""Shared FastAPI dependencies: DB session, authentication, rate limits.

Authorization lives in exactly two places so it cannot be forgotten:
  * :func:`get_current_user` / :func:`require_admin` - who may call the route;
  * :func:`document_repo.get_document` - who may see a given document
    (owner check happens in the query, and a miss returns 404, not 403).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..auth.ratelimit import login_throttle, rate_limiter
from ..auth.tokens import TokenError, decode_access_token
from ..config import Settings, get_settings
from ..db import get_session_factory
from ..models import User
from ..repositories import user_repo

WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def settings_dep() -> Settings:
    return get_settings()


def client_ip(request: Request) -> str:
    """Best-effort client identity for rate limiting (never trusted for auth)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=WWW_AUTHENTICATE,
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=WWW_AUTHENTICATE,
        )
    return token


def _user_from_token(session: Session, token: str) -> User:
    settings = get_settings()
    try:
        claims = decode_access_token(token, settings)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=WWW_AUTHENTICATE,
        ) from exc
    subject = claims.get("sub")
    if not subject or not str(subject).isdigit():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=WWW_AUTHENTICATE,
        )
    user = user_repo.get_by_id(session, int(subject))
    # The role is always read from the database, never from the token or client.
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=WWW_AUTHENTICATE,
        )
    return user


def get_current_user(
    session: Session = Depends(get_db),
    token: str = Depends(bearer_token),
) -> User:
    return _user_from_token(session, token)


def get_optional_user(
    session: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User | None:
    """Used only by rate limiting, which must work for anonymous callers too."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return _user_from_token(session, token)
    except HTTPException:
        return None


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


class ScopedRateLimit:
    """Reusable rate-limit dependency bound to a settings attribute."""

    def __init__(self, scope: str, spec_attribute: str, *, per_user: bool = False) -> None:
        self.scope = scope
        self.spec_attribute = spec_attribute
        self.per_user = per_user

    def __call__(
        self,
        request: Request,
        user: User | None = Depends(get_optional_user),
    ) -> None:
        settings = get_settings()
        spec = getattr(settings, self.spec_attribute, "60/minute")
        identity = f"user:{user.id}" if (self.per_user and user) else f"ip:{client_ip(request)}"
        allowed, retry_after = rate_limiter.check(f"{self.scope}:{identity}", spec)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )


limit_login = ScopedRateLimit("login", "rate_limit_login")
limit_upload = ScopedRateLimit("upload", "rate_limit_upload", per_user=True)
limit_reprocess = ScopedRateLimit("reprocess", "rate_limit_reprocess", per_user=True)
limit_export = ScopedRateLimit("export", "rate_limit_export", per_user=True)

__all__ = [
    "ScopedRateLimit",
    "bearer_token",
    "client_ip",
    "get_current_user",
    "get_db",
    "get_optional_user",
    "limit_export",
    "limit_login",
    "limit_reprocess",
    "limit_upload",
    "login_throttle",
    "rate_limiter",
    "require_admin",
    "settings_dep",
]

"""Authentication: register, login, current user, logout.

Password policy, hashing (argon2id), JWT issue/decode, throttle and lockout all
live in ``app.auth`` - this module only orchestrates them and writes audit
entries. ``role`` is never accepted from the client.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ...auth.passwords import hash_password, needs_rehash, validate_password_policy, verify_password
from ...auth.ratelimit import login_throttle
from ...auth.tokens import create_access_token
from ...config import Settings
from ...constants import AuditAction, Role
from ...models import User
from ...repositories import audit_repo, user_repo
from ...utils.logging_setup import get_logger
from ..deps import client_ip, get_current_user, get_db, limit_login, settings_dep
from ..errors import api_error
from ..schemas import LoginIn, RegisterIn, TokenResponse

logger = get_logger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_dummy_hash: str | None = None


def _timing_equality_hash() -> str:
    """A real argon2 hash so unknown emails cost the same as known ones."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer-placeholder-1")
    return _dummy_hash


def _issue(user: User, settings: Settings) -> TokenResponse:
    token = create_access_token(user.id, settings)
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user={
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
        },
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


@router.post("/register", status_code=201, response_model=TokenResponse)
def register(
    payload: RegisterIn,
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> TokenResponse:
    """Create an ordinary account.

    Registration **always** produces ``role=user`` - even for the address in
    ``ADMIN_EMAIL``. The administrator is created only by
    ``scripts/seed_admin.py`` (``services.admin.ensure_admin``), which runs
    server-side with the password supplied out of band. Nothing an HTTP client
    sends can influence a role.
    """
    problems = validate_password_policy(payload.password)
    if problems:
        raise api_error(422, "weak_password", " ".join(problems))
    if user_repo.get_by_email(session, payload.email) is not None:
        raise api_error(409, "email_taken", "An account with that email already exists.")

    user = user_repo.create_user(session, payload.email, hash_password(payload.password), Role.USER)
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.REGISTER,
        request_id=_request_id(request),
        detail={"role": user.role},
    )
    session.commit()
    logger.info(
        "registered new account id=%s role=%s",
        user.id,
        user.role,
        extra={"request_id": _request_id(request)},
    )
    return _issue(user, settings)



@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginIn,
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    _rate_limit: None = Depends(limit_login),
) -> TokenResponse:
    """Verify credentials and issue an access token."""
    ip = client_ip(request)
    locked, remaining = login_throttle.is_locked(payload.email, ip)
    if locked:
        raise api_error(
            429,
            "account_locked",
            f"Too many failed attempts. Try again in {remaining} seconds.",
            headers={"Retry-After": str(remaining)},
        )

    user = user_repo.get_by_email(session, payload.email)
    password_ok = verify_password(
        payload.password, user.password_hash if user is not None else _timing_equality_hash()
    )
    if user is None or not password_ok:
        login_throttle.record_failure(payload.email, ip)
        audit_repo.log_action(
            session,
            actor_user_id=user.id if user is not None else None,
            action=AuditAction.LOGIN_FAILED,
            request_id=_request_id(request),
            outcome="failure",
            detail={"reason": "invalid_credentials"},
        )
        session.commit()
        raise api_error(401, "invalid_credentials", "Email or password is incorrect.")

    if not user.is_active:
        audit_repo.log_action(
            session,
            actor_user_id=user.id,
            action=AuditAction.LOGIN_FAILED,
            request_id=_request_id(request),
            outcome="failure",
            detail={"reason": "account_disabled"},
        )
        session.commit()
        raise api_error(403, "account_disabled", "This account has been disabled.")

    if needs_rehash(user.password_hash):  # transparent upgrade after a policy change
        user.password_hash = hash_password(payload.password)
    login_throttle.reset(payload.email, ip)
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.LOGIN,
        request_id=_request_id(request),
    )
    session.commit()
    return _issue(user, settings)


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    """The caller's own account (role comes from the database, never the token)."""
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Stateless logout: tokens are short-lived and never stored server-side.

    The action is still recorded so the audit trail shows the sign-out.
    """
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.LOGOUT,
        request_id=_request_id(request),
    )
    session.commit()
    return Response(status_code=204)

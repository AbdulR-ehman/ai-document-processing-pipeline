"""Audit log access.

Append-only by design: there is no update or delete helper, and no API endpoint
exposes one. Entries never contain passwords, tokens, document contents or full
field values - only the *names* of fields that changed.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuditLog, User
from .document_repo import DEFAULT_PAGE_SIZE, clamp_page

#: Keys that must never be written to the audit log.
_FORBIDDEN_DETAIL_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "authorization",
    "secret",
    "secret_key",
    "api_key",
    "content",
    "text",
    "raw_text",
}


def log_action(
    session: Session,
    *,
    actor_user_id: int | None,
    action: str,
    target_document_id: str | None = None,
    request_id: str | None = None,
    outcome: str = "success",
    detail: dict | None = None,
) -> AuditLog:
    """Write one audit entry. ``detail`` is filtered to safe keys only."""
    safe_detail = None
    if detail:
        safe_detail = {
            key: value
            for key, value in detail.items()
            if key not in _FORBIDDEN_DETAIL_KEYS and isinstance(key, str)
        }
        safe_detail = safe_detail or None
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_document_id=target_document_id,
        request_id=request_id,
        outcome=outcome,
        detail=safe_detail,
    )
    session.add(entry)
    session.flush()
    return entry


def list_entries(
    session: Session,
    user: User,
    *,
    scope_all: bool = False,
    action: str | None = None,
    page: int | None = 1,
    page_size: int | None = DEFAULT_PAGE_SIZE,
) -> tuple[list[AuditLog], int]:
    """Users see only their own entries; admins may request the global log."""
    safe_page, safe_size = clamp_page(page, page_size)
    statement = select(AuditLog)
    if not (user.is_admin and scope_all):
        statement = statement.where(AuditLog.actor_user_id == user.id)
    if action:
        statement = statement.where(AuditLog.action == action)

    total = int(
        session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    )
    items = list(
        session.execute(
            statement.order_by(AuditLog.id.desc())
            .offset((safe_page - 1) * safe_size)
            .limit(safe_size)
        ).scalars()
    )
    return items, total

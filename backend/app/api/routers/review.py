"""Review queue: the list of documents waiting for a human decision."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...constants import DuplicateStatus, Status
from ...models import User
from ...repositories import document_repo
from ..deps import get_current_user, get_db
from ..errors import api_error
from ..schemas import Scope
from ..serializers import document_summary, page_meta

router = APIRouter(prefix="/review", tags=["review"])


def _scope_all(scope: str, user: User) -> bool:
    if scope == "all" and not user.is_admin:
        raise api_error(403, "forbidden", "Only administrators may use scope=all.")
    return scope == "all"


@router.get("/queue")
def review_queue(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    scope: Scope = "mine",
) -> dict:
    """Documents in ``NEEDS_REVIEW`` order, newest first."""
    scope_all = _scope_all(scope, user)
    items, total = document_repo.list_review_queue(
        session, user, scope_all=scope_all, page=page, page_size=page_size
    )
    safe_page, safe_size = document_repo.clamp_page(page, page_size)
    return {
        "items": [document_summary(item) for item in items],
        **page_meta(safe_page, safe_size, total),
    }


@router.get("/summary")
def review_summary(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    scope: Scope = "mine",
) -> dict:
    """Small counts block for the queue header (admin ``scope=all`` supported)."""
    scope_all = _scope_all(scope, user)
    stats = document_repo.stats_for_user(session, user, scope_all=scope_all)
    by_status = stats.get("by_status", {})
    return {
        "needs_review": by_status.get(Status.NEEDS_REVIEW, 0),
        "validation_failed": by_status.get(Status.VALIDATION_FAILED, 0),
        "duplicates": by_status.get(Status.DUPLICATE, 0),
        "flagged_duplicates": stats.get("flagged_duplicates", 0),
        "completed": by_status.get(Status.COMPLETED, 0),
        "approved": by_status.get(Status.APPROVED, 0),
        "rejected": by_status.get(Status.REJECTED, 0),
        "total": stats.get("total", 0),
        "duplicate_statuses": [DuplicateStatus.UNIQUE, DuplicateStatus.POSSIBLE_DUPLICATE, DuplicateStatus.DUPLICATE],
    }

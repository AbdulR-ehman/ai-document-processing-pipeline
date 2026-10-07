"""Document data access.

Owner scoping happens *here*, at the query level, so a route can never
accidentally leak another user's document even if it forgets a check. Admins
opt into the global scope explicitly.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..constants import DuplicateStatus, Status
from ..models import (
    Document,
    DocumentCorrection,
    DocumentExtraction,
    ProcessingRun,
    User,
    ValidationIssue,
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def clamp_page(page: int | None, page_size: int | None) -> tuple[int, int]:
    safe_page = max(1, int(page or 1))
    safe_size = int(page_size or DEFAULT_PAGE_SIZE)
    safe_size = max(1, min(MAX_PAGE_SIZE, safe_size))
    return safe_page, safe_size


def create_document(session: Session, **fields) -> Document:
    document = Document(**fields)
    session.add(document)
    session.flush()
    return document


def get_document(session: Session, document_id: str, user: User) -> Document | None:
    """Return the document only if the user may see it, else ``None`` (-> 404)."""
    if not document_id or not isinstance(document_id, str):
        return None
    statement = select(Document).where(Document.id == document_id)
    if not user.is_admin:
        statement = statement.where(Document.owner_id == user.id)
    return session.execute(statement).scalars().first()


def _filtered_statement(
    user: User,
    *,
    scope_all: bool,
    status: str | None = None,
    document_type: str | None = None,
    duplicate_status: str | None = None,
    search: str | None = None,
):
    statement = select(Document)
    if not (user.is_admin and scope_all):
        statement = statement.where(Document.owner_id == user.id)
    if status:
        statement = statement.where(Document.status == status)
    if document_type:
        statement = statement.where(Document.document_type == document_type)
    if duplicate_status:
        statement = statement.where(Document.duplicate_status == duplicate_status)
    if search:
        pattern = f"%{search.strip()[:80]}%"
        statement = statement.where(
            or_(
                Document.original_filename.ilike(pattern),
                Document.document_number.ilike(pattern),
                Document.vendor_name.ilike(pattern),
            )
        )
    return statement


def list_documents(
    session: Session,
    user: User,
    *,
    scope_all: bool = False,
    status: str | None = None,
    document_type: str | None = None,
    duplicate_status: str | None = None,
    search: str | None = None,
    page: int | None = 1,
    page_size: int | None = DEFAULT_PAGE_SIZE,
) -> tuple[list[Document], int]:
    safe_page, safe_size = clamp_page(page, page_size)
    statement = _filtered_statement(
        user,
        scope_all=scope_all,
        status=status,
        document_type=document_type,
        duplicate_status=duplicate_status,
        search=search,
    )
    total = int(
        session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    )
    items = list(
        session.execute(
            statement.order_by(Document.uploaded_at.desc(), Document.id.desc())
            .offset((safe_page - 1) * safe_size)
            .limit(safe_size)
        ).scalars()
    )
    return items, total


def list_review_queue(
    session: Session,
    user: User,
    *,
    scope_all: bool = False,
    page: int | None = 1,
    page_size: int | None = DEFAULT_PAGE_SIZE,
) -> tuple[list[Document], int]:
    return list_documents(
        session,
        user,
        scope_all=scope_all,
        status=Status.NEEDS_REVIEW,
        page=page,
        page_size=page_size,
    )


def list_approved(
    session: Session,
    user: User,
    *,
    scope_all: bool = False,
    limit: int = 5000,
) -> list[Document]:
    statement = select(Document).where(Document.status == Status.APPROVED)
    if not (user.is_admin and scope_all):
        statement = statement.where(Document.owner_id == user.id)
    return list(
        session.execute(
            statement.order_by(Document.uploaded_at.desc()).limit(max(1, min(limit, 20000)))
        ).scalars()
    )


def owner_document_count(session: Session, owner_id: int) -> int:
    return int(
        session.execute(
            select(func.count(Document.id)).where(Document.owner_id == owner_id)
        ).scalar_one()
    )


def owner_storage_bytes(session: Session, owner_id: int) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.owner_id == owner_id
            )
        ).scalar_one()
    )


def latest_extraction(session: Session, document_id: str) -> DocumentExtraction | None:
    return session.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document_id)
        .order_by(DocumentExtraction.attempt.desc(), DocumentExtraction.id.desc())
    ).scalars().first()


def validation_findings(session: Session, document_id: str) -> list[ValidationIssue]:
    return list(
        session.execute(
            select(ValidationIssue)
            .where(ValidationIssue.document_id == document_id)
            .order_by(ValidationIssue.id.asc())
        ).scalars()
    )


def clear_validation_findings(session: Session, document_id: str) -> None:
    for issue in validation_findings(session, document_id):
        session.delete(issue)


def processing_runs(session: Session, document_id: str) -> list[ProcessingRun]:
    return list(
        session.execute(
            select(ProcessingRun)
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.id.asc())
        ).scalars()
    )


def corrections(session: Session, document_id: str) -> list[DocumentCorrection]:
    return list(
        session.execute(
            select(DocumentCorrection)
            .where(DocumentCorrection.document_id == document_id)
            .order_by(DocumentCorrection.id.asc())
        ).scalars()
    )


def next_attempt_number(session: Session, document_id: str) -> int:
    runs = session.execute(
        select(func.count(ProcessingRun.id)).where(ProcessingRun.document_id == document_id)
    ).scalar_one()
    return int(runs) + 1


def stats_for_user(session: Session, user: User, *, scope_all: bool = False) -> dict:
    """Small aggregate panel: counts by status/type plus approved totals."""
    base = select(Document)
    if not (user.is_admin and scope_all):
        base = base.where(Document.owner_id == user.id)
    subquery = base.subquery()

    by_status = dict(
        session.execute(select(subquery.c.status, func.count()).group_by(subquery.c.status)).all()
    )
    by_type = dict(
        session.execute(
            select(subquery.c.document_type, func.count()).group_by(subquery.c.document_type)
        ).all()
    )
    flagged_duplicates = int(
        session.execute(
            select(func.count())
            .select_from(subquery)
            .where(subquery.c.duplicate_status != DuplicateStatus.UNIQUE)
        ).scalar_one()
    )
    return {
        "total": int(session.execute(select(func.count()).select_from(subquery)).scalar_one()),
        "by_status": {key: int(value) for key, value in by_status.items() if key},
        "by_document_type": {key: int(value) for key, value in by_type.items() if key},
        "flagged_duplicates": flagged_duplicates,
        "approved_totals": _approved_totals(session, user, scope_all),
    }


def _approved_totals(session: Session, user: User, scope_all: bool) -> dict:
    """Sum approved totals per currency (SQLite-safe: amounts summed in Python)."""
    statement = select(Document.currency, Document.total_amount).where(
        Document.status == Status.APPROVED
    )
    if not (user.is_admin and scope_all):
        statement = statement.where(Document.owner_id == user.id)
    totals: dict[str, float] = {}
    for currency, amount in session.execute(statement).all():
        try:
            value = float(amount or 0)
        except (TypeError, ValueError):
            continue
        key = currency or "N/A"
        totals[key] = round(totals.get(key, 0.0) + value, 2)
    return totals

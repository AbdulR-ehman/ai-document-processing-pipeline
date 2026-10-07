"""Reports and platform metadata: stats, audit log, export, meta."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...config import Settings
from ...constants import ALL_STATUSES, AuditAction, REVIEWABLE_STATUSES, REPROCESSABLE_STATUSES, Status
from ...models import User
from ...repositories import audit_repo, document_repo
from ...schemas.document_types import DOCUMENT_SCHEMAS
from ...utils.helpers import utcnow
from ..deps import get_current_user, get_db, limit_export, settings_dep
from ..errors import api_error
from ..schemas import ExportFormat, Scope
from ..serializers import audit_dict, document_summary, page_meta

router = APIRouter(tags=["reports"])

AUDIT_ACTIONS = frozenset(
    {
        AuditAction.REGISTER,
        AuditAction.LOGIN,
        AuditAction.LOGIN_FAILED,
        AuditAction.LOGOUT,
        AuditAction.UPLOAD,
        AuditAction.VIEW,
        AuditAction.DOWNLOAD,
        AuditAction.EDIT,
        AuditAction.APPROVE,
        AuditAction.REJECT,
        AuditAction.REPROCESS,
        AuditAction.DELETE,
        AuditAction.EXPORT,
    }
)

#: Cells starting with these characters can be interpreted as formulas by
#: spreadsheet software; a leading apostrophe neutralises them (OWASP).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _scope_all(scope: str, user: User) -> bool:
    if scope == "all" and not user.is_admin:
        raise api_error(403, "forbidden", "Only administrators may use scope=all.")
    return scope == "all"


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in _FORMULA_PREFIXES:
        try:
            float(text)  # plain numbers (including negatives) are safe
            return text
        except ValueError:
            return "'" + text
    return text


@router.get("/stats")
def stats(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
    scope: Scope = "mine",
) -> dict:
    """Aggregate counts for the dashboard (own documents by default)."""
    scope_all = _scope_all(scope, user)
    data = document_repo.stats_for_user(session, user, scope_all=scope_all)
    by_status = data.setdefault("by_status", {})
    data["needs_review"] = by_status.get(Status.NEEDS_REVIEW, 0)
    data["processed"] = sum(
        by_status.get(key, 0)
        for key in (
            Status.COMPLETED,
            Status.NEEDS_REVIEW,
            Status.VALIDATION_FAILED,
            Status.DUPLICATE,
            Status.APPROVED,
            Status.REJECTED,
            Status.EXTRACTION_FAILED,
            Status.ERROR,
        )
    )
    data["storage_bytes"] = (
        None if scope_all else document_repo.owner_storage_bytes(session, user.id)
    )
    data["quotas"] = {
        "max_documents": settings.max_docs_per_user,
        "max_storage_bytes": settings.max_storage_bytes_per_user,
        "used_documents": data["total"] if not scope_all else None,
    }
    return data


@router.get("/audit")
def audit_log(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    action: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    scope: Scope = "mine",
) -> dict:
    """Append-only audit trail (admins may read the global log with scope=all)."""
    if action is not None and action not in AUDIT_ACTIONS:
        raise api_error(422, "unknown_action", f"Unknown audit action '{action[:32]}'.")
    scope_all = _scope_all(scope, user)
    entries, total = audit_repo.list_entries(
        session, user, scope_all=scope_all, action=action, page=page, page_size=page_size
    )
    safe_page, safe_size = document_repo.clamp_page(page, page_size)
    return {
        "items": [audit_dict(entry) for entry in entries],
        **page_meta(safe_page, safe_size, total),
    }


@router.get("/export")
def export_documents(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
    format: ExportFormat = "csv",
    scope: Scope = "mine",
    _rate_limit: None = Depends(limit_export),
) -> Response:
    """Export APPROVED documents as CSV (spreadsheet-safe) or JSON."""
    scope_all = _scope_all(scope, user)
    rows = document_repo.list_approved(
        session, user, scope_all=scope_all, limit=settings.export_max_rows
    )
    documents = [document_summary(item) for item in rows]

    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.EXPORT,
        detail={"format": format, "rows": len(documents)},
    )
    session.commit()

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if format == "json":
        return JSONResponse(
            content={
                "exported_at": utcnow().isoformat(),
                "count": len(documents),
                "documents": documents,
            },
            headers={
                "Content-Disposition": f'attachment; filename="documents-export-{stamp}.json"'
            },
        )

    columns = [
        "id",
        "original_filename",
        "document_type",
        "document_number",
        "vendor_name",
        "document_date",
        "total_amount",
        "currency",
        "status",
        "validation_status",
        "duplicate_status",
        "uploaded_at",
        "reviewed_at",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for item in documents:
        writer.writerow([_csv_safe(item.get(column)) for column in columns])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="documents-export-{stamp}.csv"'
        },
    )


@router.get("/meta")
def meta(settings: Settings = Depends(settings_dep)) -> dict:
    """Public, secret-free description of what this API accepts."""
    return {
        "app": "AI Document Processing Pipeline",
        "api_version": "v1",
        "ai_provider": settings.ai_provider,
        "document_types": sorted(DOCUMENT_SCHEMAS),
        "statuses": sorted(ALL_STATUSES),
        "reviewable_statuses": sorted(REVIEWABLE_STATUSES),
        "reprocessable_statuses": sorted(REPROCESSABLE_STATUSES),
        "duplicate_statuses": ["unique", "possible_duplicate", "duplicate"],
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_pdf_pages": settings.max_pdf_pages,
            "max_text_chars": settings.max_text_chars,
            "max_line_items": 200,
            "export_max_rows": settings.export_max_rows,
        },
    }

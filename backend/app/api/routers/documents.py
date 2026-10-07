"""Document routes: upload, list, detail, source text, download, reprocess,
corrections, approve/reject and delete.

Authorization is enforced inside the repository query
(``document_repo.get_document``), so a caller can only ever see their own
documents: a miss returns 404, which never confirms that somebody else's id
exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from sqlalchemy.orm import Session
from starlette.responses import FileResponse

from ...ai.provider import get_provider
from ...config import Settings
from ...constants import ALL_STATUSES, AuditAction, Status, transition_allowed
from ...documents.extraction import extract_text
from ...documents.validation import ALLOWED_EXTENSIONS, UploadValidationError, sanitize_filename
from ...models import Document, User
from ...repositories import audit_repo, document_repo
from ...schemas.document_types import DOCUMENT_SCHEMAS
from ...services.pipeline import run_pipeline
from ...services.review import ReviewError, apply_correction, decide
from ...services.storage import StorageError, read_stored_file, delete_stored_file
from ...services.uploads import store_upload
from ..deps import get_current_user, get_db, limit_reprocess, limit_upload, settings_dep
from ..errors import api_error
from ..schemas import ApproveIn, CorrectionIn, RejectIn, Scope
from ..serializers import document_detail, document_summary, page_meta

router = APIRouter(prefix="/documents", tags=["documents"])

DUPLICATE_VALUES = ("unique", "possible_duplicate", "duplicate")


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _scope_all(scope: str, user: User) -> bool:
    if scope == "all" and not user.is_admin:
        raise api_error(403, "forbidden", "Only administrators may use scope=all.")
    return scope == "all"


def _get_document(session: Session, document_id: str, user: User) -> Document:
    document = document_repo.get_document(session, document_id, user)
    if document is None:
        raise api_error(404, "not_found", "Document not found.")
    return document


def _detail_payload(
    session: Session, document: Document, *, include_raw: bool = False
) -> dict:
    return {
        "document": document_detail(
            document,
            findings=document_repo.validation_findings(session, document.id),
            runs=document_repo.processing_runs(session, document.id),
            corrections=document_repo.corrections(session, document.id),
            extraction=document_repo.latest_extraction(session, document.id),
            include_raw=include_raw,
        )
    }


def _run_and_report(
    session: Session,
    document: Document,
    settings: Settings,
    request: Request,
    *,
    action: str,
) -> dict:
    """Execute the pipeline for one document and write the audit entry."""
    result = run_pipeline(
        session,
        document,
        get_provider(settings),
        settings,
        request_id=_rid(request),
    )
    audit_repo.log_action(
        session,
        actor_user_id=document.owner_id,
        action=action,
        target_document_id=document.id,
        request_id=_rid(request),
        detail={
            "filename": document.original_filename[:80],
            "status": result.status,
        },
    )
    session.commit()
    return {
        "document": document_summary(document),
        "stages": result.stages,
        "errors": result.errors,
        "status": result.status,
    }


@router.post("/upload", status_code=201)
def upload_document(
    request: Request,
    file: UploadFile = File(..., description="PDF, TXT or Markdown file"),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
    _rate_limit: None = Depends(limit_upload),
) -> dict:
    """Store an uploaded file and run the full pipeline over it synchronously."""
    try:
        document = store_upload(session, user, file, settings)
        session.commit()
    except UploadValidationError as exc:
        session.rollback()
        audit_repo.log_action(
            session,
            actor_user_id=user.id,
            action=AuditAction.UPLOAD,
            request_id=_rid(request),
            outcome="failure",
            detail={"code": exc.code},
        )
        session.commit()
        raise api_error(exc.status_code, exc.code, exc.message) from exc
    return _run_and_report(session, document, settings, request, action=AuditAction.UPLOAD)


@router.get("")
def list_documents(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status: str | None = Query(default=None, description="Lifecycle status filter"),
    document_type: str | None = Query(default=None, description="invoice | receipt | purchase_order"),
    duplicate_status: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=80, description="Filename / number / vendor"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    scope: Scope = "mine",
) -> dict:
    """Paginated document list (own documents; admins may use ``scope=all``)."""
    if status is not None and status not in ALL_STATUSES:
        raise api_error(422, "unknown_status", f"Unknown status '{status[:24]}'.")
    if document_type is not None and document_type not in DOCUMENT_SCHEMAS:
        raise api_error(422, "unknown_document_type", f"Unknown document type '{document_type[:24]}'.")
    if duplicate_status is not None and duplicate_status not in DUPLICATE_VALUES:
        raise api_error(422, "unknown_duplicate_status", "Unknown duplicate status.")
    scope_all = _scope_all(scope, user)

    items, total = document_repo.list_documents(
        session,
        user,
        scope_all=scope_all,
        status=status,
        document_type=document_type,
        duplicate_status=duplicate_status,
        search=search,
        page=page,
        page_size=page_size,
    )
    safe_page, safe_size = document_repo.clamp_page(page, page_size)
    return {
        "items": [document_summary(item) for item in items],
        **page_meta(safe_page, safe_size, total),
    }


@router.get("/{document_id}")
def get_document(
    document_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    include_raw: bool = Query(default=False, description="Include raw AI output in the response"),
) -> dict:
    """Full detail: extracted fields, findings, run history and corrections."""
    document = _get_document(session, document_id, user)
    return _detail_payload(session, document, include_raw=include_raw)


@router.get("/{document_id}/text")
def get_document_text(
    document_id: str,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
) -> dict:
    """Extracted source text, re-read from the stored file on demand.

    This is what the evidence panel checks values against; reading it is an
    audited action because it exposes document content.
    """
    document = _get_document(session, document_id, user)
    try:
        stored = read_stored_file(settings.upload_dir, document.stored_filename)
    except StorageError as exc:
        raise api_error(404, "file_missing", "The stored file is no longer available.") from exc

    result = extract_text(
        stored,
        ALLOWED_EXTENSIONS.get(document.file_extension, ""),
        max_pages=settings.max_pdf_pages,
        max_chars=settings.max_text_chars,
        timeout_seconds=settings.extraction_timeout_seconds,
    )
    if not result.text.strip():
        raise api_error(
            422,
            "no_text",
            result.user_message or "No text could be extracted from this document.",
        )
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.VIEW,
        target_document_id=document.id,
        request_id=_rid(request),
        detail={"kind": "source_text"},
    )
    session.commit()
    return {
        "text": result.text,
        "page_count": result.page_count,
        "status": result.status,
        "truncated": len(result.text) >= settings.max_text_chars,
    }


@router.get("/{document_id}/download")
def download_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """Download the original bytes (server-generated path, containment checked)."""
    document = _get_document(session, document_id, user)
    try:
        stored = read_stored_file(settings.upload_dir, document.stored_filename)
    except StorageError as exc:
        raise api_error(404, "file_missing", "The stored file is no longer available.") from exc

    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.DOWNLOAD,
        target_document_id=document.id,
        request_id=_rid(request),
    )
    session.commit()
    return FileResponse(
        path=stored,
        media_type=document.detected_content_type or "application/octet-stream",
        filename=sanitize_filename(document.original_filename),
    )


@router.post("/{document_id}/reprocess")
def reprocess_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
    _rate_limit: None = Depends(limit_reprocess),
) -> dict:
    """Re-run the whole pipeline (text extraction -> AI -> checks) once more."""
    document = _get_document(session, document_id, user)
    if document.status == Status.PROCESSING or not transition_allowed(
        document.status, Status.PROCESSING
    ):
        raise api_error(
            409,
            "invalid_state",
            f"A document with status {document.status} cannot be reprocessed.",
        )
    return _run_and_report(session, document, settings, request, action=AuditAction.REPROCESS)


@router.patch("/{document_id}/corrections")
def correct_document_field(
    document_id: str,
    payload: CorrectionIn,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
) -> dict:
    """Correct one extracted field; checks re-run immediately (no new AI call).

    The audit entry records only the *name* of the field that changed - never
    the old or new value (those live in ``document_corrections``).
    """
    document = _get_document(session, document_id, user)
    try:
        apply_correction(
            session,
            document,
            settings,
            field_path=payload.field_path,
            value=payload.value,
            actor=user,
        )
    except ReviewError as exc:
        session.rollback()
        raise api_error(exc.status_code, exc.code, exc.message) from exc
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.EDIT,
        target_document_id=document.id,
        request_id=_rid(request),
        detail={"fields": [payload.field_path], "status": document.status},
    )
    session.commit()
    document = _get_document(session, document_id, user)
    return _detail_payload(session, document)


def _review_decision(
    session: Session,
    document: Document,
    user: User,
    request: Request,
    *,
    approve: bool,
    note: str | None,
) -> dict:
    target = Status.APPROVED if approve else Status.REJECTED
    if document.status == target:
        raise api_error(
            409,
            "already_decided",
            f"This document is already {'approved' if approve else 'rejected'}.",
        )
    try:
        decide(session, document, actor=user, approve=approve, note=note)
    except ReviewError as exc:
        session.rollback()
        raise api_error(exc.status_code, exc.code, exc.message) from exc
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        target_document_id=document.id,
        request_id=_rid(request),
        detail={"status": document.status},
    )
    session.commit()
    return _detail_payload(session, document)


@router.post("/{document_id}/approve")
def approve_document(
    document_id: str,
    request: Request,
    payload: ApproveIn | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Mark a document as approved (owner or admin)."""
    document = _get_document(session, document_id, user)
    return _review_decision(
        session, document, user, request, approve=True, note=payload.note if payload else None
    )


@router.post("/{document_id}/reject")
def reject_document(
    document_id: str,
    payload: RejectIn,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Reject a document with a required, auditable reason."""
    document = _get_document(session, document_id, user)
    return _review_decision(
        session, document, user, request, approve=False, note=payload.reason
    )


@router.delete("/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """Delete the record and its stored file (never while processing)."""
    document = _get_document(session, document_id, user)
    if document.status == Status.PROCESSING:
        raise api_error(
            409, "invalid_state", "A document cannot be deleted while it is processing."
        )
    filename = document.original_filename
    stored_filename = document.stored_filename
    document_id = document.id  # keep id for the audit row after the delete
    delete_stored_file(settings.upload_dir, stored_filename)
    session.delete(document)
    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.DELETE,
        target_document_id=document_id,
        request_id=_rid(request),
        detail={"filename": filename[:80]},
    )
    session.commit()
    return Response(status_code=204)

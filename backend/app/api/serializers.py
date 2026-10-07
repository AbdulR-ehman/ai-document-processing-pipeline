"""JSON-safe serializers for API responses.

Plain dicts rather than Pydantic models: the shape of a document detail
depends on what exists (findings, runs, corrections) and these are assembled
once here so every route returns the same contract.

Nothing here ever includes passwords, tokens, raw file bytes or other users'
data.
"""

from __future__ import annotations

import math
from typing import Any

from ..constants import REVIEWABLE_STATUSES, Status, transition_allowed
from ..models import (
    AuditLog,
    Document,
    DocumentCorrection,
    DocumentExtraction,
    ProcessingRun,
    User,
    ValidationIssue,
)


def iso(value) -> str | None:
    """ISO-8601 timestamp (or ``None``)."""
    return value.isoformat() if value else None


def user_public(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": iso(user.created_at),
    }


def document_summary(document: Document) -> dict[str, Any]:
    """The columns shown in list views."""
    return {
        "id": document.id,
        "original_filename": document.original_filename,
        "file_size": document.file_size,
        "file_extension": document.file_extension,
        "document_type": document.document_type,
        "document_number": document.document_number,
        "vendor_name": document.vendor_name,
        "document_date": document.document_date,
        "total_amount": document.total_amount,
        "currency": document.currency,
        "status": document.status,
        "validation_status": document.validation_status,
        "duplicate_status": document.duplicate_status,
        "duplicate_of": document.duplicate_of,
        "duplicate_signals": list(document.duplicate_signals or []),
        "error_message": document.error_message,
        "processing_ms": document.processing_ms,
        "ai_provider": document.ai_provider,
        "page_count": document.page_count,
        "text_length": document.text_length,
        "uploaded_at": iso(document.uploaded_at),
        "updated_at": iso(document.updated_at),
        "reviewed_at": iso(document.reviewed_at),
        "reviewed_by": document.reviewed_by,
        "rejection_reason": document.rejection_reason,
        "owner_id": document.owner_id,
    }


def document_actions(document: Document) -> dict[str, bool]:
    """Which actions the *caller* may attempt - the server re-checks every one."""
    return {
        "can_reprocess": transition_allowed(document.status, Status.PROCESSING),
        "can_correct": document.status in REVIEWABLE_STATUSES,
        "can_approve": document.status != Status.APPROVED
        and transition_allowed(document.status, Status.APPROVED),
        "can_reject": document.status != Status.REJECTED
        and transition_allowed(document.status, Status.REJECTED),
        "can_delete": document.status != Status.PROCESSING,
    }


def finding_dict(finding: ValidationIssue) -> dict[str, Any]:
    return {
        "id": finding.id,
        "stage": finding.stage,
        "severity": finding.severity,
        "code": finding.code,
        "field": finding.field,
        "message": finding.message,
        "created_at": iso(finding.created_at),
    }


def run_dict(run: ProcessingRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "attempt": run.attempt,
        "provider": run.provider,
        "status": run.status,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "duration_ms": run.duration_ms,
        "stages": list(run.stages or []),
        "error_message": run.error_message,
    }


def correction_dict(correction: DocumentCorrection) -> dict[str, Any]:
    return {
        "id": correction.id,
        "field_path": correction.field_path,
        "original_value": correction.original_value,
        "corrected_value": correction.corrected_value,
        "corrected_by": correction.corrected_by,
        "created_at": iso(correction.created_at),
    }


def extraction_dict(
    extraction: DocumentExtraction | None, *, include_raw: bool = False
) -> dict[str, Any] | None:
    if extraction is None:
        return None
    payload: dict[str, Any] = {
        "id": extraction.id,
        "attempt": extraction.attempt,
        "provider": extraction.provider,
        "status": extraction.status,
        "error_message": extraction.error_message,
        "created_at": iso(extraction.created_at),
        "data": extraction.data,
        # Raw provider output can be large and echoes untrusted content, so it
        # is only returned when explicitly requested.
        "has_raw_output": bool(extraction.raw_output),
    }
    if include_raw:
        payload["raw_output"] = extraction.raw_output
    return payload


def document_detail(
    document: Document,
    *,
    findings: list[ValidationIssue],
    runs: list[ProcessingRun],
    corrections: list[DocumentCorrection],
    extraction: DocumentExtraction | None,
    include_raw: bool = False,
) -> dict[str, Any]:
    detail = document_summary(document)
    detail["actions"] = document_actions(document)
    detail["findings"] = [finding_dict(item) for item in findings]
    detail["runs"] = [run_dict(item) for item in runs]
    detail["corrections"] = [correction_dict(item) for item in corrections]
    detail["extraction"] = extraction_dict(extraction, include_raw=include_raw)
    return detail


def audit_dict(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "timestamp": iso(entry.timestamp),
        "actor_user_id": entry.actor_user_id,
        "action": entry.action,
        "target_document_id": entry.target_document_id,
        "request_id": entry.request_id,
        "outcome": entry.outcome,
        "detail": entry.detail,
    }


def page_meta(page: int, page_size: int, total: int) -> dict[str, int]:
    pages = max(1, math.ceil(total / page_size)) if page_size else 1
    return {"page": page, "page_size": page_size, "total": total, "total_pages": pages}

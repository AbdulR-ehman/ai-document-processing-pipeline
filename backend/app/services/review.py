"""Human review actions: corrections and approve/reject decisions.

Each function either succeeds or raises :class:`ReviewError` with a safe code
and message; the router converts it into an HTTP response. Corrections are the
only way extracted data is ever changed by a person, and every change is kept
in ``document_corrections`` (old value retained) plus the audit log.
"""

from __future__ import annotations

import json

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import Settings
from ..constants import REVIEWABLE_STATUSES, Status, transition_allowed
from ..models import Document, DocumentCorrection, User
from ..repositories import document_repo
from ..schemas.document_types import get_schema
from ..utils.helpers import utcnow
from .pipeline import RevalidateError, revalidate_document


class ReviewError(ValueError):
    """Safe, user-facing failure for a review action."""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


#: Corrections may only set scalar values - structure (line items) is edited
#: field by field, never replaced wholesale.
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _segments(field_path: str) -> list[str]:
    return [part for part in field_path.split(".") if part]


def _descend(container, segment: str, walked: list[str]):
    if isinstance(container, dict):
        if segment in container:
            return container[segment]
        raise ReviewError(
            "unknown_field",
            f"Field '{'.'.join(walked)}' does not exist in the extracted data.",
            422,
        )
    if isinstance(container, list):
        if not segment.isdigit() or int(segment) >= len(container):
            raise ReviewError(
                "unknown_field",
                f"Index '{segment}' is out of range for '{'.'.join(walked)}'.",
                422,
            )
        return container[int(segment)]
    raise ReviewError(
        "unknown_field", f"Field '{'.'.join(walked)}' cannot be edited.", 422
    )


def _parent(payload: dict, segments: list[str]):
    current = payload
    for position, segment in enumerate(segments[:-1]):
        current = _descend(current, segment, segments[: position + 1])
    return current, segments[-1]


def get_value(payload: dict, segments: list[str]):
    container, key = _parent(payload, segments)
    if isinstance(container, dict):
        if key not in container:
            raise ReviewError("unknown_field", f"Field '{key}' does not exist.", 422)
        return container[key]
    if isinstance(container, list):
        if not key.isdigit() or int(key) >= len(container):
            raise ReviewError("unknown_field", f"Index '{key}' is out of range.", 422)
        return container[int(key)]
    raise ReviewError("unknown_field", "That field cannot be edited.", 422)


def set_value(payload: dict, segments: list[str], value) -> None:
    container, key = _parent(payload, segments)
    if not isinstance(value, _SCALAR_TYPES) or isinstance(value, (dict, list)):
        raise ReviewError(
            "invalid_value",
            "A correction must be a text, number, true/false or null value.",
            422,
        )
    if isinstance(container, dict):
        if key not in container:
            raise ReviewError("unknown_field", f"Field '{key}' does not exist.", 422)
        container[key] = value
        return
    if isinstance(container, list):
        if not key.isdigit() or int(key) >= len(container):
            raise ReviewError("unknown_field", f"Index '{key}' is out of range.", 422)
        container[int(key)] = value
        return
    raise ReviewError("unknown_field", "That field cannot be edited.", 422)


def apply_correction(
    session: Session,
    document: Document,
    settings: Settings,
    *,
    field_path: str,
    value,
    actor: User,
) -> Document:
    """Correct one extracted field and re-run every check (no AI call)."""
    if document.status not in REVIEWABLE_STATUSES:
        raise ReviewError(
            "not_reviewable",
            f"A document with status {document.status} cannot be corrected.",
        )
    extraction = document_repo.latest_extraction(session, document.id)
    if extraction is None or not extraction.data:
        raise ReviewError(
            "no_extracted_data",
            "This document has no extracted data to correct yet.",
        )

    segments = _segments(field_path)
    if not segments:
        raise ReviewError("unknown_field", "The field path is empty.", 422)

    payload = dict(extraction.data)
    original = get_value(payload, segments)
    if _as_text(original) == _as_text(value) and type(original) is type(value):
        return document  # nothing to do - never write an empty correction

    set_value(payload, segments, value)

    schema = get_schema(document.document_type)
    if schema is None:
        raise ReviewError("unsupported_type", "The document type is not supported.", 409)
    try:
        payload = schema(**payload).model_dump(mode="json")
    except ValidationError as exc:
        fields = [
            ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            for error in exc.errors()[:5]
        ]
        raise ReviewError(
            "invalid_value",
            "The corrected value does not match the document schema (check: "
            + ", ".join(fields)
            + ").",
            422,
        ) from exc

    # Persist the payload *and* the human-readable trail of the change.
    extraction.data = payload
    session.add(
        DocumentCorrection(
            document_id=document.id,
            field_path=field_path,
            original_value=_as_text(original),
            corrected_value=_as_text(value),
            corrected_by=actor.id,
            created_at=utcnow(),
        )
    )
    session.flush()

    # Re-run schema/business/evidence/duplicate checks with the corrected data.
    try:
        revalidate_document(session, document, settings, trigger=f"correction:{field_path}")
    except RevalidateError as exc:
        raise ReviewError("revalidate_failed", str(exc), 409) from exc
    return document


def decide(
    session: Session,
    document: Document,
    *,
    actor: User,
    approve: bool,
    note: str | None = None,
) -> Document:
    """Approve or reject a document (owner or admin, decided by the router)."""
    target = Status.APPROVED if approve else Status.REJECTED
    if not transition_allowed(document.status, target):
        raise ReviewError(
            "invalid_transition",
            f"A document with status {document.status} cannot be "
            f"{'approved' if approve else 'rejected'}.",
        )
    document.status = target
    document.reviewed_at = utcnow()
    document.reviewed_by = actor.id
    document.rejection_reason = None if approve else (note or "Rejected during review")[:300]
    session.flush()
    return document



def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):  # pragma: no cover - scalars always work
        return str(value)

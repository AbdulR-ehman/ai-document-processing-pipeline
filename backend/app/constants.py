"""Shared enumerations and status transition rules.

Kept in one small module so the pipeline, API and tests all agree on the same
vocabulary.
"""

from __future__ import annotations


class Status:
    """Document lifecycle statuses."""

    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


#: Every status the app will persist. Anything else is a bug.
ALL_STATUSES: frozenset[str] = frozenset(
    {
        Status.UPLOADED,
        Status.PROCESSING,
        Status.COMPLETED,
        Status.NEEDS_REVIEW,
        Status.VALIDATION_FAILED,
        Status.EXTRACTION_FAILED,
        Status.DUPLICATE,
        Status.ERROR,
        Status.APPROVED,
        Status.REJECTED,
    }
)

#: Statuses where no further automatic processing will happen.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {Status.APPROVED, Status.REJECTED, Status.DUPLICATE}
)

#: Statuses that may be re-run by the reprocess endpoint.
REPROCESSABLE_STATUSES: frozenset[str] = frozenset(
    {Status.EXTRACTION_FAILED, Status.VALIDATION_FAILED, Status.ERROR, Status.UPLOADED}
)

#: Statuses that may be corrected in the review workflow.
REVIEWABLE_STATUSES: frozenset[str] = frozenset(
    {Status.NEEDS_REVIEW, Status.COMPLETED, Status.VALIDATION_FAILED, Status.DUPLICATE}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    Status.UPLOADED: frozenset({Status.PROCESSING, Status.ERROR}),
    Status.PROCESSING: frozenset(
        {
            Status.COMPLETED,
            Status.NEEDS_REVIEW,
            Status.VALIDATION_FAILED,
            Status.EXTRACTION_FAILED,
            Status.DUPLICATE,
            Status.ERROR,
        }
    ),
    Status.COMPLETED: frozenset(
        {
            Status.NEEDS_REVIEW,
            Status.VALIDATION_FAILED,
            Status.APPROVED,
            Status.REJECTED,
            Status.PROCESSING,
            Status.ERROR,
        }
    ),
    Status.NEEDS_REVIEW: frozenset(
        {
            Status.APPROVED,
            Status.REJECTED,
            Status.COMPLETED,
            Status.VALIDATION_FAILED,
            Status.NEEDS_REVIEW,
            Status.PROCESSING,
            Status.ERROR,
        }
    ),
    Status.VALIDATION_FAILED: frozenset(
        {Status.NEEDS_REVIEW, Status.COMPLETED, Status.DUPLICATE, Status.PROCESSING, Status.ERROR}
    ),
    Status.EXTRACTION_FAILED: frozenset({Status.PROCESSING, Status.ERROR}),
    Status.DUPLICATE: frozenset(
        {
            Status.NEEDS_REVIEW,
            Status.COMPLETED,
            Status.APPROVED,
            Status.REJECTED,
            Status.PROCESSING,
            Status.ERROR,
        }
    ),
    Status.ERROR: frozenset({Status.PROCESSING, Status.ERROR}),
    Status.APPROVED: frozenset({Status.PROCESSING, Status.NEEDS_REVIEW}),
    Status.REJECTED: frozenset({Status.PROCESSING, Status.NEEDS_REVIEW, Status.APPROVED}),
}


def transition_allowed(current: str, target: str) -> bool:
    """Return True when ``current -> target`` is a legal status change."""
    if target not in ALL_STATUSES:
        return False
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class DuplicateStatus:
    UNIQUE = "unique"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    DUPLICATE = "duplicate"


class ValidationStatus:
    PASSED = "passed"
    WARNINGS = "warnings"
    ERRORS = "errors"


class Severity:
    ERROR = "error"
    WARNING = "warning"


class Role:
    USER = "user"
    ADMIN = "admin"


class AuditAction:
    REGISTER = "register"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    UPLOAD = "upload"
    VIEW = "view"
    DOWNLOAD = "download"
    EDIT = "edit"
    APPROVE = "approve"
    REJECT = "reject"
    REPROCESS = "reprocess"
    DELETE = "delete"
    EXPORT = "export"

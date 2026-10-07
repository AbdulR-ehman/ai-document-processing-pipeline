"""Upload handling: validate -> check quota -> stream to disk -> create record.

The order matters. The filename and extension are validated *before* anything is
written, the file is streamed with a hard size cap, and the bytes are sniffed
afterwards. The stored name is always a server-generated UUID.
"""

from __future__ import annotations

from fastapi import UploadFile
from sqlalchemy.orm import Session

from ..config import Settings
from ..documents.validation import (
    UploadValidationError,
    validate_declared_mime,
    validate_extension,
    validate_original_filename,
    sanitize_filename,
    sniff_content,
)
from ..models import Document, User
from ..repositories import document_repo
from ..utils.helpers import new_id
from .storage import StorageError, delete_stored_file, save_stream


class QuotaExceeded(UploadValidationError):
    """Raised when the user is over their document or storage quota."""

    def __init__(self, message: str) -> None:
        super().__init__("quota_exceeded", message, 413)


def store_upload(
    session: Session,
    user: User,
    upload: UploadFile,
    settings: Settings,
) -> Document:
    """Persist one uploaded file and return the created (unprocessed) document."""
    # 1. Filename / extension allowlist (no disk writes yet).
    base = validate_original_filename(upload.filename)
    extension, kind = validate_extension(base)

    # 2. Per-user quotas.
    if document_repo.owner_document_count(session, user.id) >= settings.max_docs_per_user:
        raise QuotaExceeded(
            f"You have reached the limit of {settings.max_docs_per_user} documents."
        )
    if (
        document_repo.owner_storage_bytes(session, user.id) + settings.max_upload_bytes
        > settings.max_storage_bytes_per_user
    ):
        raise QuotaExceeded("Your storage quota is full. Delete some documents first.")

    # 3. Stream to disk under a random name, aborting past the size cap.
    try:
        stored = save_stream(upload.file, settings.upload_dir, extension, settings.max_upload_bytes)
    except StorageError as exc:
        if "maximum" in str(exc):
            raise UploadValidationError(
                "too_large",
                f"The file is larger than the {settings.max_upload_mb} MB limit.",
                413,
            ) from exc
        if "empty" in str(exc):
            raise UploadValidationError("empty_file", "The uploaded file is empty.", 400) from exc
        raise UploadValidationError("storage_error", "The file could not be stored.", 500) from exc

    # 4. Content sniff + declared MIME check. On failure the file is removed.
    try:
        if stored.size > settings.max_upload_bytes:
            raise UploadValidationError(
                "too_large",
                f"The file is larger than the {settings.max_upload_mb} MB limit.",
                413,
            )
        validate_declared_mime(kind, upload.content_type)
        sniff_content(stored.probe, kind)
    except UploadValidationError:
        delete_stored_file(settings.upload_dir, stored.stored_filename)
        raise

    from ..documents.validation import DETECTED_CONTENT_TYPES

    document = document_repo.create_document(
        session,
        id=new_id(),
        owner_id=user.id,
        original_filename=sanitize_filename(base),
        stored_filename=stored.stored_filename,
        file_hash=stored.sha256,
        file_size=stored.size,
        file_extension=extension,
        declared_content_type=(upload.content_type or "")[:120] or None,
        detected_content_type=DETECTED_CONTENT_TYPES[kind],
        document_type=None,
        status="UPLOADED",
    )
    return document

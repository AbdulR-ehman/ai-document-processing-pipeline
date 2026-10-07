"""Document and processing-related models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..constants import DuplicateStatus, Status
from ..db import Base
from ..utils.helpers import utcnow


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- file metadata (sanitized) ---
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_extension: Mapped[str] = mapped_column(String(10), nullable=False)
    declared_content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detected_content_type: Mapped[str] = mapped_column(String(120), nullable=False)

    # --- extraction summary (denormalized for listing/filtering) ---
    document_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    document_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vendor_normalized: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    document_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    total_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- pipeline state ---
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=Status.UPLOADED, index=True
    )
    validation_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duplicate_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DuplicateStatus.UNIQUE
    )
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    duplicate_signals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # --- review workflow ---
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    owner: Mapped["User"] = relationship(back_populates="documents")  # noqa: F821
    extractions: Mapped[list["DocumentExtraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    validations: Mapped[list["ValidationIssue"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list["ProcessingRun"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    corrections: Mapped[list["DocumentCorrection"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_documents_owner_hash", "owner_id", "file_hash"),
        Index("ix_documents_owner_number", "owner_id", "document_number"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document id={self.id} status={self.status} owner={self.owner_id}>"


class DocumentExtraction(Base):
    """One AI extraction attempt (raw output + parsed/validated payload)."""

    __tablename__ = "document_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    document: Mapped["Document"] = relationship(back_populates="extractions")


class ValidationIssue(Base):
    """One schema, business or evidence finding for a document."""

    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "schema" | "business" | "evidence"
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    document: Mapped["Document"] = relationship(back_populates="validations")


class ProcessingRun(Base):
    """One execution of the pipeline over a document (history)."""

    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [{"name": "...", "status": "...", "detail": "..."}]
    stages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    document: Mapped["Document"] = relationship(back_populates="runs")


class DocumentCorrection(Base):
    """A human correction of one extracted field (original value retained)."""

    __tablename__ = "document_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_path: Mapped[str] = mapped_column(String(120), nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    document: Mapped["Document"] = relationship(back_populates="corrections")


class ExtractionAttemptStatus:
    """Values stored in ``document_extractions.status``."""

    OK = "ok"
    INVALID_JSON = "invalid_json"
    SCHEMA_ERROR = "schema_error"
    PROVIDER_ERROR = "provider_error"

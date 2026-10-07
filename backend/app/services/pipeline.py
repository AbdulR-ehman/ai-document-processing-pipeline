"""The processing pipeline.

Ordinary functions called in a fixed order. Each stage is independently
testable and a failure in one document can never affect another:

  extract text -> preprocess -> classify -> AI extract -> schema validate
  -> business validate -> evidence check -> duplicate check -> persist

The AI call is a single ``provider.extract(text, document_type)`` invocation
with at most ONE validation-guided retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..ai.provider import AIProvider, AIProviderError
from ..config import Settings
from ..constants import DuplicateStatus, Severity, Status, ValidationStatus
from ..documents.classifier import classify
from ..documents.evidence import check_evidence, unsupported_fields
from ..documents.extraction import TextExtractionStatus, extract_text
from ..documents.preprocessing import preprocess_text
from ..duplicate_detection.detector import detect_duplicates
from ..models import Document, DocumentExtraction, ProcessingRun, ValidationIssue
from ..repositories import document_repo
from ..schemas.document_types import get_schema, summarize
from ..utils.helpers import utcnow
from ..validation.business import summarise as summarise_validation
from ..validation.business import validate_business

#: Text extraction statuses that mean "we could not obtain any text".
FAILED_EXTRACTION_STATUSES = {
    TextExtractionStatus.EMPTY,
    TextExtractionStatus.SCANNED_NO_TEXT,
    TextExtractionStatus.ENCRYPTED,
    TextExtractionStatus.CORRUPTED,
    TextExtractionStatus.TOO_MANY_PAGES,
    TextExtractionStatus.TIMEOUT,
    TextExtractionStatus.UNSUPPORTED,
}

MAX_RAW_OUTPUT_CHARS = 20_000
DEFAULT_DOCUMENT_TYPE = "invoice"


class StageStatus:
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PipelineResult:
    document_id: str
    status: str
    stages: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    extracted: dict | None = None

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "status": self.status,
            "stages": self.stages,
            "errors": self.errors,
        }


def _schema_error_summary(exc: ValidationError) -> str:
    """Compact, safe description of a schema failure (never echoes values)."""
    parts = []
    for error in exc.errors()[:10]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('type', 'invalid')}")
    return "; ".join(parts) or "schema validation failed"


def _safe_text(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _record_extraction(
    session: Session,
    document: Document,
    attempt: int,
    provider: AIProvider,
    status: str,
    raw_output: str | None,
    data: dict | None,
    error_message: str | None,
) -> DocumentExtraction:
    extraction = DocumentExtraction(
        document_id=document.id,
        attempt=attempt,
        provider=provider.name,
        status=status,
        raw_output=raw_output,
        data=data,
        error_message=_safe_text(error_message),
    )
    session.add(extraction)
    session.flush()
    return extraction


def _record_issue(
    session: Session,
    document: Document,
    stage: str,
    severity: str,
    code: str,
    field_name: str | None,
    message: str,
) -> None:
    session.add(
        ValidationIssue(
            document_id=document.id,
            stage=stage,
            severity=severity,
            code=code,
            field=field_name,
            message=_safe_text(message, 480) or "",
        )
    )


def _extract_with_retry(
    session: Session,
    document: Document,
    provider: AIProvider,
    text: str,
    document_type: str,
    attempt_base: int,
    stages: list[dict],
) -> tuple[dict | None, str | None]:
    """Call the provider, validate against the schema, retry once on failure."""
    schema = get_schema(document_type)
    if schema is None:
        return None, f"unsupported document type: {document_type}"

    last_error: str | None = None
    for attempt in (1, 2):
        try:
            raw = provider.extract(text, document_type, last_error)
        except AIProviderError as exc:
            _record_extraction(
                session, document, attempt_base + attempt - 1, provider,
                "provider_error", None, None, str(exc),
            )
            last_error = f"provider error: {exc}"
            continue

        if isinstance(raw, (bytes, str)):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                _record_extraction(
                    session, document, attempt_base + attempt - 1, provider,
                    "invalid_json", str(raw)[:MAX_RAW_OUTPUT_CHARS], None,
                    f"invalid JSON: {type(exc).__name__}",
                )
                last_error = "output was not valid JSON"
                continue

        if not isinstance(raw, dict):
            last_error = "output was not a JSON object"
            _record_extraction(
                session, document, attempt_base + attempt - 1, provider,
                "invalid_json", None, None, last_error,
            )
            continue

        try:
            model = schema(**raw)
        except ValidationError as exc:
            summary = _schema_error_summary(exc)
            _record_extraction(
                session, document, attempt_base + attempt - 1, provider,
                "schema_error", json.dumps(raw, default=str)[:MAX_RAW_OUTPUT_CHARS],
                None, summary,
            )
            last_error = summary
            continue

        payload = model.model_dump(mode="json")
        _record_extraction(
            session, document, attempt_base + attempt - 1, provider, "ok",
            json.dumps(raw, default=str)[:MAX_RAW_OUTPUT_CHARS], payload, None,
        )
        if attempt == 2:
            stages.append({
                "name": "ai_extraction",
                "status": StageStatus.WARNING,
                "detail": "succeeded after one validation-guided retry",
            })
        return payload, None

    return None, last_error or "extraction failed"


def compute_status(
    *,
    business_errors: bool,
    warnings: bool,
    unsupported: bool,
    duplicate_status: str,
) -> str:
    """Decide the final document status from the pipeline signals.

    Priority: hard validation errors, then anything a human should look at
    (warnings / unsupported evidence / a possible duplicate -> NEEDS_REVIEW),
    then an exact duplicate, and finally a clean COMPLETED.
    """
    if business_errors:
        return Status.VALIDATION_FAILED
    if warnings or unsupported or duplicate_status == DuplicateStatus.POSSIBLE_DUPLICATE:
        return Status.NEEDS_REVIEW
    if duplicate_status == DuplicateStatus.DUPLICATE:
        return Status.DUPLICATE
    return Status.COMPLETED


def run_pipeline(
    session: Session,
    document: Document,
    provider: AIProvider,
    settings: Settings,
    *,
    file_path=None,
    request_id: str | None = None,  # noqa: ARG001 - reserved for richer logging
) -> PipelineResult:
    """Run the full pipeline for one document and persist everything.

    Commits when it finishes. An unexpected error is caught, recorded on the
    document as ``ERROR`` with a safe message, and never propagates to the
    caller - one bad document must not break the app or other documents.
    """
    started = utcnow()
    stages: list[dict] = []
    result = PipelineResult(document_id=document.id, status=document.status)
    attempt = document_repo.next_attempt_number(session, document.id)

    try:
        return _run_stages(
            session, document, provider, settings, file_path, started, stages, result, attempt
        )
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        session.rollback()
        document = session.get(Document, result.document_id) or document
        result.errors.append("An internal error occurred while processing this document.")
        result.stages = stages
        document.status = Status.ERROR
        document.error_message = f"internal error ({type(exc).__name__})"
        document.processing_ms = int((utcnow() - started).total_seconds() * 1000)
        session.add(
            ProcessingRun(
                document_id=document.id,
                attempt=attempt,
                provider=provider.name,
                status=Status.ERROR,
                started_at=started,
                finished_at=utcnow(),
                duration_ms=document.processing_ms,
                stages=stages,
                error_message=document.error_message,
            )
        )
        session.commit()
        result.status = Status.ERROR
        return result


def _finish_run(
    session: Session,
    document: Document,
    provider: AIProvider,
    stages: list[dict],
    result: PipelineResult,
    attempt: int,
    started,
    target_status: str,
    error_message: str | None = None,
) -> PipelineResult:
    """Persist the processing run and final document state."""
    from ..constants import transition_allowed

    if transition_allowed(document.status, target_status):
        document.status = target_status
    else:  # pragma: no cover - defensive: never leave an invalid status
        result.errors.append(f"Invalid status transition {document.status} -> {target_status}")
        document.status = Status.ERROR
        target_status = Status.ERROR

    finished = utcnow()
    document.processing_ms = int((finished - started).total_seconds() * 1000)
    document.error_message = _safe_text(error_message)
    document.updated_at = finished

    session.add(
        ProcessingRun(
            document_id=document.id,
            attempt=attempt,
            provider=provider.name,
            status=document.status,
            started_at=started,
            finished_at=finished,
            duration_ms=document.processing_ms,
            stages=stages,
            error_message=document.error_message,
        )
    )
    session.commit()
    result.status = document.status
    result.stages = stages
    return result


def _run_stages(
    session: Session,
    document: Document,
    provider: AIProvider,
    settings: Settings,
    file_path,
    started,
    stages: list[dict],
    result: PipelineResult,
    attempt: int,
) -> PipelineResult:
    from ..constants import transition_allowed
    from ..documents.validation import ALLOWED_EXTENSIONS

    # --- reset + enter PROCESSING ----------------------------------------
    document_repo.clear_validation_findings(session, document.id)
    if transition_allowed(document.status, Status.PROCESSING):
        document.status = Status.PROCESSING
    document.ai_provider = provider.name
    document.error_message = None
    session.flush()

    kind = ALLOWED_EXTENSIONS.get(document.file_extension, "")
    path = file_path or (settings.upload_dir / document.stored_filename)

    # --- 1. text extraction ----------------------------------------------
    extraction = extract_text(
        path,
        kind,
        max_pages=settings.max_pdf_pages,
        max_chars=settings.max_text_chars,
        timeout_seconds=settings.extraction_timeout_seconds,
    )
    document.page_count = extraction.page_count
    document.text_length = len(extraction.text)
    stages.append(
        {
            "name": "text_extraction",
            "status": StageStatus.OK if extraction.ok else StageStatus.FAILED,
            "detail": extraction.status,
        }
    )
    if extraction.status in FAILED_EXTRACTION_STATUSES:
        message = extraction.user_message or "Text extraction failed."
        _record_issue(
            session, document, "extraction", Severity.ERROR,
            f"extraction_{extraction.status}", None, message,
        )
        result.errors.append(message)
        return _finish_run(
            session, document, provider, stages, result, attempt, started,
            Status.EXTRACTION_FAILED, error_message=message,
        )

    # --- 2. preprocessing -------------------------------------------------
    text = preprocess_text(extraction.text, settings.ai_max_input_chars)
    if not text.strip():
        message = "The document contained no usable text."
        _record_issue(
            session, document, "extraction", Severity.ERROR, "extraction_empty", None, message
        )
        result.errors.append(message)
        stages.append({"name": "preprocessing", "status": StageStatus.FAILED, "detail": message})
        return _finish_run(
            session, document, provider, stages, result, attempt, started,
            Status.EXTRACTION_FAILED, error_message=message,
        )
    stages.append(
        {"name": "preprocessing", "status": StageStatus.OK, "detail": f"{len(text)} characters"}
    )

    # --- 3. classification ------------------------------------------------
    classification = classify(text)
    document_type = classification.document_type or DEFAULT_DOCUMENT_TYPE
    if classification.document_type is None:
        _record_issue(
            session, document, "schema", Severity.WARNING, "document_type_uncertain",
            "document_type", "The document type could not be determined.",
        )
    document.document_type = document_type
    stages.append(
        {
            "name": "classification",
            "status": StageStatus.OK if classification.document_type else StageStatus.WARNING,
            "detail": f"{document_type} (confidence {classification.confidence})",
        }
    )

    return _run_ai_and_checks(
        session, document, provider, settings, text, document_type, attempt,
        started, stages, result,
    )


def _run_ai_and_checks(
    session: Session,
    document: Document,
    provider: AIProvider,
    settings: Settings,
    text: str,
    document_type: str,
    attempt: int,
    started,
    stages: list[dict],
    result: PipelineResult,
) -> PipelineResult:
    # --- 4/5. AI extraction + schema validation (at most one retry) -------
    payload, extraction_error = _extract_with_retry(
        session, document, provider, text, document_type, attempt * 10, stages
    )
    if payload is None:
        message = extraction_error or "The extracted data did not match the expected schema."
        _record_issue(
            session, document, "schema", Severity.ERROR, "schema_validation_failed", None, message
        )
        result.errors.append("The extracted data did not match the expected schema.")
        stages.append(
            {"name": "schema_validation", "status": StageStatus.FAILED, "detail": message}
        )
        return _finish_run(
            session, document, provider, stages, result, attempt, started,
            Status.EXTRACTION_FAILED, error_message=f"schema validation failed: {message}",
        )
    if not any(entry["name"] == "ai_extraction" for entry in stages):
        stages.append(
            {
                "name": "ai_extraction",
                "status": StageStatus.OK,
                "detail": f"provider={provider.name}",
            }
        )
    stages.append({"name": "schema_validation", "status": StageStatus.OK, "detail": "valid"})
    result.extracted = payload
    return _run_checks(
        session, document, provider, settings, payload, text, document_type,
        attempt, started, stages, result,
    )


def _run_checks(
    session: Session,
    document: Document,
    provider: AIProvider,
    settings: Settings,
    payload: dict,
    text: str,
    document_type: str,
    attempt: int,
    started,
    stages: list[dict],
    result: PipelineResult,
) -> PipelineResult:
    """Stages 6-9: business validation, evidence, duplicates, final status.

    Shared by the normal pipeline run and by :func:`revalidate_document` (which
    re-runs these checks after a human correction without calling the AI again).
    """
    # --- 6. business validation ------------------------------------------
    findings = validate_business(payload, document_type, settings.business_total_tolerance)
    for finding in findings:
        _record_issue(
            session, document, finding.stage, finding.severity, finding.code,
            finding.field, finding.message,
        )
    document.validation_status = summarise_validation(findings)
    has_errors = any(finding.severity == Severity.ERROR for finding in findings)
    has_warnings = any(finding.severity == Severity.WARNING for finding in findings)
    stages.append(
        {
            "name": "business_validation",
            "status": (
                StageStatus.FAILED if has_errors
                else StageStatus.WARNING if has_warnings
                else StageStatus.OK
            ),
            "detail": f"{len(findings)} finding(s)",
        }
    )

    # --- 7. evidence check (anti-hallucination) ---------------------------
    unsupported = unsupported_fields(check_evidence(payload, text, document_type))
    for finding in unsupported:
        _record_issue(
            session, document, "evidence", Severity.WARNING, "unsupported_by_source",
            finding.field,
            f"The value for '{finding.field}' was not found in the source document.",
        )
    stages.append(
        {
            "name": "evidence_check",
            "status": StageStatus.WARNING if unsupported else StageStatus.OK,
            "detail": f"{len(unsupported)} field(s) unsupported by source",
        }
    )

    # --- 8. duplicate detection -------------------------------------------
    summary = summarize(payload, document_type)
    document.document_number = summary["document_number"]
    document.document_date = summary["document_date"]
    document.vendor_name = summary["vendor_name"]
    document.vendor_normalized = summary["vendor_normalized"]
    document.total_amount = summary["total_amount"]
    document.currency = summary["currency"]

    duplicate = detect_duplicates(
        session,
        document.owner_id,
        file_hash=document.file_hash,
        document_number=summary["document_number"],
        vendor_normalized=summary["vendor_normalized"],
        document_date=summary["document_date"],
        total_amount=summary["total_amount"],
        exclude_document_id=document.id,
    )
    document.duplicate_status = duplicate.status
    document.duplicate_of = duplicate.matched_document_id
    document.duplicate_signals = duplicate.signals or None
    stages.append(
        {
            "name": "duplicate_check",
            "status": StageStatus.WARNING if duplicate.is_flagged else StageStatus.OK,
            "detail": duplicate.status,
        }
    )

    # --- 9. final status --------------------------------------------------
    target = compute_status(
        business_errors=has_errors,
        warnings=has_warnings,
        unsupported=bool(unsupported),
        duplicate_status=duplicate.status,
    )
    stages.append({"name": "storage", "status": StageStatus.OK, "detail": target})
    return _finish_run(session, document, provider, stages, result, attempt, started, target)


class _StaticProvider:
    """Minimal provider stand-in used when re-running checks (no AI call)."""

    def __init__(self, name: str) -> None:
        self.name = name


class RevalidateError(ValueError):
    """Raised when stored data cannot be re-validated (safe message)."""


def revalidate_document(
    session: Session,
    document: Document,
    settings: Settings,
    *,
    trigger: str = "correction",
) -> PipelineResult:
    """Re-run schema/business/evidence/duplicate checks on stored data.

    Used after a human correction: the AI is *not* called again, the stored
    payload is simply re-checked and the document status recomputed. Raises
    :class:`RevalidateError` if the stored payload no longer matches the schema
    (the API turns that into a 422).
    """
    started = utcnow()
    attempt = document_repo.next_attempt_number(session, document.id)
    stages: list[dict] = [
        {"name": "revalidate", "status": StageStatus.OK, "detail": trigger}
    ]
    result = PipelineResult(document_id=document.id, status=document.status)

    extraction = document_repo.latest_extraction(session, document.id)
    if extraction is None or not extraction.data:
        raise RevalidateError("There is no extracted data to re-validate for this document.")

    payload = dict(extraction.data)
    document_type = document.document_type or DEFAULT_DOCUMENT_TYPE
    schema = get_schema(document_type)
    if schema is None:
        raise RevalidateError("The document type is not supported.")

    try:
        payload = schema(**payload).model_dump(mode="json")
    except ValidationError as exc:
        raise RevalidateError(_schema_error_summary(exc)) from exc

    stages.append({"name": "schema_validation", "status": StageStatus.OK, "detail": "valid"})
    document_repo.clear_validation_findings(session, document.id)

    # The evidence check compares the payload against the source text, so the
    # text is re-read from the stored file (still no AI call).
    from ..documents.validation import ALLOWED_EXTENSIONS

    kind = ALLOWED_EXTENSIONS.get(document.file_extension, "")
    extraction_result = extract_text(
        settings.upload_dir / document.stored_filename,
        kind,
        max_pages=settings.max_pdf_pages,
        max_chars=settings.max_text_chars,
        timeout_seconds=settings.extraction_timeout_seconds,
    )
    if extraction_result.status in FAILED_EXTRACTION_STATUSES or not extraction_result.text.strip():
        raise RevalidateError("The source text for this document is no longer available.")
    text = preprocess_text(extraction_result.text, settings.ai_max_input_chars)

    provider = _StaticProvider(document.ai_provider or "mock")
    result.extracted = payload
    return _run_checks(
        session, document, provider, settings, payload, text, document_type,
        attempt, started, stages, result,
    )

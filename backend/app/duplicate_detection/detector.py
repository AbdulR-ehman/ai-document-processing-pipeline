"""Duplicate detection.

Signals, in decreasing order of strength:
  1. identical file content (SHA-256) uploaded by the same owner -> ``duplicate``
  2. the same document number, strengthened by matching vendor / date / total

Rules (deliberately small and unit-tested):
  * checks are always scoped to a single owner, so one user can never learn that
    another user's document exists;
  * documents are never rejected, only flagged, with the signals and the matched
    document id recorded;
  * vendor names are normalized before comparison (case, accents, punctuation,
    whitespace);
  * ``matched_signals >= 3`` -> duplicate, ``== 2`` -> possible_duplicate,
    otherwise unique.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import DuplicateStatus
from ..models import Document

#: score at or above which we call it a duplicate rather than a possible one
DUPLICATE_THRESHOLD = 3
POSSIBLE_THRESHOLD = 2


@dataclass
class DuplicateResult:
    status: str = DuplicateStatus.UNIQUE
    score: int = 0
    matched_document_id: str | None = None
    signals: list[dict] = field(default_factory=list)

    @property
    def is_flagged(self) -> bool:
        return self.status != DuplicateStatus.UNIQUE


def _candidate_query(session: Session, owner_id: int, exclude_document_id: str | None):
    statement = select(Document).where(Document.owner_id == owner_id)
    if exclude_document_id:
        statement = statement.where(Document.id != exclude_document_id)
    return statement


def _same_number_candidates(
    session: Session,
    owner_id: int,
    document_number: str | None,
    exclude_document_id: str | None,
    vendor_normalized: str | None,
    document_date: str | None,
    total_amount: str | None,
) -> list[Document]:
    """Candidate documents that could be the same document as this one.

    The lookup itself is intentionally narrow (same number, or - when there is
    no number - the same vendor/date/total) so unrelated documents are never
    compared, and the extra signals only decide the severity.
    """
    statement = _candidate_query(session, owner_id, exclude_document_id)
    if document_number:
        statement = statement.where(Document.document_number == document_number)
    elif vendor_normalized and document_date and total_amount:
        statement = statement.where(
            Document.vendor_normalized == vendor_normalized,
            Document.document_date == document_date,
            Document.total_amount == total_amount,
        )
    else:
        return []
    return list(session.execute(statement.limit(10)).scalars())


def detect_duplicates(
    session: Session,
    owner_id: int,
    *,
    file_hash: str,
    document_number: str | None,
    vendor_normalized: str | None,
    document_date: str | None,
    total_amount: str | None,
    exclude_document_id: str | None = None,
) -> DuplicateResult:
    """Classify this document against the owner's existing documents."""

    # 1. Identical bytes by the same owner is an unambiguous duplicate.
    identical = session.execute(
        _candidate_query(session, owner_id, exclude_document_id).where(
            Document.file_hash == file_hash
        )
    ).scalars().first()
    if identical is not None:
        return DuplicateResult(
            status=DuplicateStatus.DUPLICATE,
            score=99,
            matched_document_id=identical.id,
            signals=[{"name": "file_hash", "matched": True, "detail": "identical file content"}],
        )

    # 2. Same document number, scored by how many additional signals agree.
    best = DuplicateResult()
    for candidate in _same_number_candidates(
        session,
        owner_id,
        document_number,
        exclude_document_id,
        vendor_normalized,
        document_date,
        total_amount,
    ):
        signals: list[dict] = []
        score = 0

        if document_number and candidate.document_number == document_number:
            score += 1
            signals.append({"name": "document_number", "matched": True})
        if vendor_normalized and candidate.vendor_normalized == vendor_normalized:
            score += 1
            signals.append({"name": "vendor", "matched": True})
        if document_date and candidate.document_date == document_date:
            score += 1
            signals.append({"name": "document_date", "matched": True})
        if total_amount and candidate.total_amount == total_amount:
            score += 1
            signals.append({"name": "total_amount", "matched": True})

        if score > best.score:
            best = DuplicateResult(
                status=(
                    DuplicateStatus.DUPLICATE
                    if score >= DUPLICATE_THRESHOLD
                    else DuplicateStatus.POSSIBLE_DUPLICATE
                ),
                score=score,
                matched_document_id=candidate.id,
                signals=[{**signal, "candidate": candidate.id} for signal in signals],
            )

    if best.score >= POSSIBLE_THRESHOLD:
        return best
    return DuplicateResult(status=DuplicateStatus.UNIQUE, score=best.score, signals=[])

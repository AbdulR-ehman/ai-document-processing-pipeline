"""Unit tests for the pure building blocks (no HTTP, no fixtures)."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.ratelimit import InvalidLimitSpec, LoginThrottle, RateLimiter, parse_limit
from app.constants import DuplicateStatus, Status, transition_allowed
from app.documents.validation import UploadValidationError, validate_extension, validate_original_filename
from app.services.pipeline import compute_status


# --- status decisions -------------------------------------------------------
def test_compute_status_priority():
    assert compute_status(
        business_errors=True, warnings=True, unsupported=True,
        duplicate_status=DuplicateStatus.DUPLICATE,
    ) == Status.VALIDATION_FAILED


def test_compute_status_review_wins_over_duplicate():
    assert compute_status(
        business_errors=False, warnings=True, unsupported=False,
        duplicate_status=DuplicateStatus.DUPLICATE,
    ) == Status.NEEDS_REVIEW


def test_compute_status_possible_duplicate_needs_review():
    assert compute_status(
        business_errors=False, warnings=False, unsupported=False,
        duplicate_status=DuplicateStatus.POSSIBLE_DUPLICATE,
    ) == Status.NEEDS_REVIEW


def test_compute_status_exact_duplicate_and_clean():
    assert compute_status(
        business_errors=False, warnings=False, unsupported=False,
        duplicate_status=DuplicateStatus.DUPLICATE,
    ) == Status.DUPLICATE
    assert compute_status(
        business_errors=False, warnings=False, unsupported=False,
        duplicate_status=DuplicateStatus.UNIQUE,
    ) == Status.COMPLETED


# --- status transitions -----------------------------------------------------
def test_transition_allowed_happy_path():
    assert transition_allowed(Status.UPLOADED, Status.PROCESSING)
    assert transition_allowed(Status.NEEDS_REVIEW, Status.APPROVED)
    assert transition_allowed(Status.NEEDS_REVIEW, Status.REJECTED)
    assert transition_allowed(Status.VALIDATION_FAILED, Status.DUPLICATE)


def test_transition_rejects_illegal_moves():
    assert not transition_allowed(Status.APPROVED, Status.REJECTED)
    assert not transition_allowed(Status.UPLOADED, Status.APPROVED)
    assert not transition_allowed(Status.COMPLETED, Status.UPLOADED)
    assert not transition_allowed(Status.NEEDS_REVIEW, "HACKED")


# --- upload validation ------------------------------------------------------
def test_validate_extension_accepts_known_types():
    assert validate_extension("report.txt") == (".txt", "txt")
    assert validate_extension("report.TXT") == (".txt", "txt")
    assert validate_extension("report.pdf") == (".pdf", "pdf")
    assert validate_extension("report.md") == (".md", "md")


def test_validate_extension_rejects_everything_else():
    for name in ("report.exe", "report.html", "report", "report.tar.gz"):
        with pytest.raises(UploadValidationError) as excinfo:
            validate_extension(name)
        assert excinfo.value.status_code == 415


def test_validate_extension_handles_bare_extension_names():
    # Regression: a bare "txt" (no dot) used to slip through un-checked.
    with pytest.raises(UploadValidationError):
        validate_extension("txt")


def test_filename_validation_rejects_paths_and_controls():
    for bad in ("../../etc/passwd.txt", "..\\..\\boot.ini.txt", "weird\x00name.txt", "/etc/hosts.txt"):
        with pytest.raises(UploadValidationError) as excinfo:
            validate_original_filename(bad)
        assert excinfo.value.status_code == 400
    # Ordinary names pass through unchanged.
    assert validate_original_filename("My Invoice (1).txt") == "My Invoice (1).txt"


# --- rate limiting / lockout ------------------------------------------------
def test_parse_limit():
    assert parse_limit("5/minute") == (5, 60)
    assert parse_limit("10/hour") == (10, 3600)
    assert parse_limit(" 3 /seconds ") == (3, 1)
    for bad in ("", "soon", "5/day", "five/minute"):
        with pytest.raises(InvalidLimitSpec):
            parse_limit(bad)


def test_rate_limiter_sliding_window():
    limiter = RateLimiter()
    assert limiter.check("key", "2/minute")[0] is True
    assert limiter.check("key", "2/minute")[0] is True
    allowed, retry_after = limiter.check("key", "2/minute")
    assert allowed is False
    assert retry_after >= 1
    limiter.reset("key")
    assert limiter.check("key", "2/minute")[0] is True


def test_login_throttle_locks_after_threshold():
    throttle = LoginThrottle(max_failures=2, lockout_seconds=30)
    assert throttle.is_locked("u1") == (False, 0)
    throttle.record_failure("u1")
    assert throttle.is_locked("u1") == (False, 0)
    throttle.record_failure("u1")
    locked, remaining = throttle.is_locked("u1")
    assert locked is True and remaining > 0
    throttle.reset("u1")
    assert throttle.is_locked("u1") == (False, 0)


def test_login_throttle_configure_from_settings():
    throttle = LoginThrottle()
    throttle.configure(max_failures=3, lockout_seconds=45)
    assert throttle.max_failures == 3
    assert throttle.lockout_seconds == 45


# --- passwords --------------------------------------------------------------
def test_password_policy_rules():
    from app.auth.passwords import validate_password_policy

    assert validate_password_policy("Passw0rdTest1") == []
    assert any("at least" in p for p in validate_password_policy("ab1"))
    assert any("digit" in p for p in validate_password_policy("abcdefghijkl"))
    assert any("letter" in p for p in validate_password_policy("1234567890"))


def test_password_hash_is_one_way_and_verifies():
    from app.auth.passwords import hash_password, verify_password

    stored = hash_password("Passw0rdTest1")
    assert stored != "Passw0rdTest1"
    assert stored.startswith("$argon2")
    assert verify_password("Passw0rdTest1", stored) is True
    assert verify_password("wrong-password", stored) is False
    assert verify_password("anything", "not-a-real-hash") is False


# --- business validation ----------------------------------------------------
def _invoice(total="110") -> dict:
    return {
        "document_type": "invoice",
        "invoice_number": "INV-100",
        "invoice_date": "2024-03-01",
        "due_date": "2024-03-31",
        "vendor": {"name": "Acme Corp"},
        "currency": "USD",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50, "total": 100}],
        "subtotal": 100,
        "tax": 10,
        "total": total,
    }


def test_business_validation_accepts_consistent_totals():
    from app.validation.business import validate_business

    findings = validate_business(_invoice(), "invoice", 0.01)
    assert findings == []


def test_business_validation_flags_inconsistent_totals():
    from app.validation.business import validate_business, summarise

    findings = validate_business(_invoice(total="999"), "invoice", 0.01)
    assert findings, "expected a finding for the broken total"
    assert summarise(findings) in ("errors", "warnings")


# --- evidence check ---------------------------------------------------------
def test_evidence_finds_values_present_in_source():
    from app.documents.evidence import check_evidence, unsupported_fields

    payload = _invoice()
    text = "INVOICE INV-100 dated 2024-03-01\nAcme Corp\nWidget 2 x 50.00 = 100.00\nSubtotal 100.00 Tax 10.00 Total $110.00"
    unsupported = unsupported_fields(check_evidence(payload, text, "invoice"))
    fields = {finding.field for finding in unsupported}
    assert "invoice_number" not in fields
    assert "total" not in fields


def test_evidence_flags_fabricated_values():
    from app.documents.evidence import check_evidence, unsupported_fields

    payload = _invoice()
    payload["invoice_number"] = "INV-FABRICATED-999"
    text = "INVOICE INV-100\nTotal $110.00"
    unsupported = unsupported_fields(check_evidence(payload, text, "invoice"))
    fields = {finding.field for finding in unsupported}
    assert "invoice_number" in fields


# --- label parsing (mock provider) -----------------------------------------
RECEIPT_LABELS = ("receipt number", "receipt no", "receipt #", "receipt")


def test_labeled_value_does_not_steal_a_neighbouring_label():
    """Regression: "Receipt Date: ..." used to be read as the receipt number."""
    from app.ai.mock_provider import _labeled

    text = "RECEIPT\n\nReceipt Date: 2024-04-06\nPayment Method: VISA\n"
    assert _labeled(text, RECEIPT_LABELS) is None
    assert _labeled("Receipt Date: 2024-04-06", ("receipt date", "date")) == "2024-04-06"


def test_labeled_value_supports_the_usual_formats():
    from app.ai.mock_provider import _labeled

    assert _labeled("Receipt Number: RCP-9001", RECEIPT_LABELS) == "RCP-9001"
    assert _labeled("Receipt No. 9002", RECEIPT_LABELS) == "9002"
    assert _labeled("Receipt #9003", RECEIPT_LABELS) == "9003"
    assert _labeled("Receipt 9004", RECEIPT_LABELS) == "9004"
    # A label with no value is a missing value, not a value on the next line.
    assert _labeled("Receipt Number:\n", RECEIPT_LABELS) is None

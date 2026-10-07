"""Business rule validation.

Kept strictly separate from schema validation (which lives in the Pydantic
models). Two severities are produced:

  * ``error``   - the document fails validation (status VALIDATION_FAILED)
  * ``warning`` - the document is usable but needs a human look (NEEDS_REVIEW)

Every finding records the stage, a stable code, the field and a safe message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..constants import Severity
from ..schemas.document_types import DATE_FIELD, NUMBER_FIELD, SECONDARY_DATE_FIELD
from ..utils.currency import is_valid_currency

STAGE = "business"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/.]*$")

MIN_YEAR = 1990
MAX_YEAR = 2100

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%Y%m%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
)


@dataclass(frozen=True)
class ValidationFinding:
    stage: str
    severity: str
    code: str
    field: str | None
    message: str

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "severity": self.severity,
            "code": self.code,
            "field": self.field,
            "message": self.message,
        }


def _error(code: str, field: str | None, message: str) -> ValidationFinding:
    return ValidationFinding(STAGE, Severity.ERROR, code, field, message)


def _warning(code: str, field: str | None, message: str) -> ValidationFinding:
    return ValidationFinding(STAGE, Severity.WARNING, code, field, message)


def parse_date(raw: str | None) -> date | None:
    """Parse a date string using the accepted formats. Returns None if invalid."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip().rstrip(".,;")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _check_number(data: dict, document_type: str, findings: list[ValidationFinding]) -> None:
    field = NUMBER_FIELD.get(document_type, "invoice_number")
    value = data.get(field)
    if not value:
        findings.append(_warning("missing_document_number", field, "No document number was found."))
        return
    if not NUMBER_RE.match(value):
        findings.append(
            _warning(
                "document_number_format",
                field,
                "The document number contains unexpected characters.",
            )
        )


def _check_dates(data: dict, document_type: str, findings: list[ValidationFinding]) -> None:
    primary_field = DATE_FIELD.get(document_type)
    secondary_field = SECONDARY_DATE_FIELD.get(document_type)
    primary = parse_date(data.get(primary_field)) if primary_field else None

    if primary_field:
        raw = data.get(primary_field)
        if not raw:
            findings.append(
                _warning("missing_document_date", primary_field, "No document date was found.")
            )
        elif primary is None:
            findings.append(
                _warning("date_unparseable", primary_field, "The document date could not be understood.")
            )
        elif not (MIN_YEAR <= primary.year <= MAX_YEAR):
            findings.append(
                _warning(
                    "date_out_of_range",
                    primary_field,
                    f"The document date is outside the plausible range ({MIN_YEAR}-{MAX_YEAR}).",
                )
            )

    if not secondary_field:
        return
    secondary_raw = data.get(secondary_field)
    if not secondary_raw:
        return
    secondary = parse_date(secondary_raw)
    if secondary is None:
        findings.append(
            _warning("date_unparseable", secondary_field, "A date could not be understood.")
        )
        return
    if not (MIN_YEAR <= secondary.year <= MAX_YEAR):
        findings.append(
            _warning(
                "date_out_of_range",
                secondary_field,
                f"A date is outside the plausible range ({MIN_YEAR}-{MAX_YEAR}).",
            )
        )
    if primary is not None and secondary < primary:
        findings.append(
            _error(
                "date_order",
                secondary_field,
                "The secondary date is earlier than the document date.",
            )
        )


def _check_currency(data: dict, findings: list[ValidationFinding]) -> None:
    currency = data.get("currency")
    if not currency:
        findings.append(_warning("missing_currency", "currency", "No currency was found."))
        return
    if not is_valid_currency(currency):
        findings.append(
            _error("invalid_currency", "currency", "The currency is not a valid ISO 4217 code.")
        )


def _check_amounts(data: dict, findings: list[ValidationFinding]) -> None:
    for field in ("subtotal", "tax", "total"):
        amount = _as_decimal(data.get(field))
        if amount is not None and amount < 0:
            findings.append(_error("negative_amount", field, "The amount is negative."))

    for index, item in enumerate(data.get("line_items") or []):
        for field in ("quantity", "unit_price", "total"):
            amount = _as_decimal(item.get(field))
            if amount is not None and amount < 0:
                findings.append(
                    _error(
                        "negative_line_amount",
                        f"line_items[{index}].{field}",
                        "A line item value is negative.",
                    )
                )

    if data.get("total") is None:
        findings.append(_warning("missing_total", "total", "No total amount was found."))


def _check_arithmetic(data: dict, tolerance: Decimal, findings: list[ValidationFinding]) -> None:
    items = data.get("line_items") or []
    decimals = [
        (
            _as_decimal(item.get("quantity")),
            _as_decimal(item.get("unit_price")),
            _as_decimal(item.get("total")),
        )
        for item in items
    ]

    for index, (quantity, unit_price, line_total) in enumerate(decimals):
        if quantity is None or unit_price is None or line_total is None:
            continue
        if abs(quantity * unit_price - line_total) > tolerance:
            findings.append(
                _warning(
                    "line_total_mismatch",
                    f"line_items[{index}].total",
                    "The line total does not match quantity x unit price.",
                )
            )

    subtotal = _as_decimal(data.get("subtotal"))
    tax = _as_decimal(data.get("tax"))
    total = _as_decimal(data.get("total"))

    line_totals = [entry[2] for entry in decimals if entry[2] is not None]
    if subtotal is not None and line_totals and len(line_totals) == len(decimals):
        if abs(sum(line_totals) - subtotal) > tolerance:
            findings.append(
                _warning(
                    "subtotal_mismatch",
                    "subtotal",
                    "The subtotal does not match the sum of the line totals.",
                )
            )

    if subtotal is not None and tax is not None and total is not None:
        if abs((subtotal + tax) - total) > tolerance:
            findings.append(
                _warning("total_mismatch", "total", "The total does not match subtotal + tax.")
            )


def _check_contacts(data: dict, findings: list[ValidationFinding]) -> None:
    for party_field in ("vendor", "customer", "ship_to"):
        party = data.get(party_field)
        if not isinstance(party, dict):
            continue
        email = party.get("email")
        if email and not EMAIL_RE.match(str(email)):
            findings.append(
                _warning(
                    "invalid_email",
                    f"{party_field}.email",
                    "An email address looks malformed.",
                )
            )


def _check_party_presence(data: dict, document_type: str, findings: list[ValidationFinding]) -> None:
    """A document without a counterparty is incomplete - warn, never invent one."""
    if document_type not in {"invoice", "receipt", "purchase_order"}:
        return
    party = data.get("vendor")
    name = party.get("name") if isinstance(party, dict) else None
    if not (isinstance(name, str) and name.strip()):
        findings.append(
            _warning(
                "missing_vendor",
                "vendor.name",
                "No vendor was found on the document.",
            )
        )


def validate_business(
    data: dict,
    document_type: str,
    tolerance: float = 0.01,
) -> list[ValidationFinding]:
    """Run every business rule and return all findings (possibly empty)."""
    findings: list[ValidationFinding] = []
    if not isinstance(data, dict) or not data:
        return [_error("empty_extraction", None, "No data was extracted from the document.")]

    tol = _as_decimal(tolerance) or Decimal("0.01")
    _check_number(data, document_type, findings)
    _check_dates(data, document_type, findings)
    _check_currency(data, findings)
    _check_amounts(data, findings)
    _check_arithmetic(data, tol, findings)
    _check_contacts(data, findings)
    _check_party_presence(data, document_type, findings)
    return findings


def summarise(findings: list[ValidationFinding]) -> str:
    """Overall validation status: errors > warnings > passed."""
    if any(finding.severity == Severity.ERROR for finding in findings):
        return "errors"
    if any(finding.severity == Severity.WARNING for finding in findings):
        return "warnings"
    return "passed"

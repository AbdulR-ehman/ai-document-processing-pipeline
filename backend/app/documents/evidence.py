"""Evidence check: verify that extracted values actually exist in the source.

Pure deterministic code - no AI. For every scalar field the AI returned we look
for the value in the document text after normalizing whitespace, case,
thousands separators, currency symbols and common date formats. Fields that
cannot be located are reported as ``unsupported_by_source`` so a human can
review them (the classic sign of a hallucinated value).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

_DATE_INPUT_FORMATS = (
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

_CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "\u20ac",
    "GBP": "\u00a3",
    "INR": "\u20b9",
    "JPY": "\u00a5",
    "CAD": "CA$",
    "AUD": "A$",
    "CHF": "CHF",
    "CNY": "\u00a5",
    "SEK": "kr",
    "BRL": "R$",
    "MXN": "MX$",
    "ZAR": "R",
    "AED": "AED",
    "SGD": "S$",
    "NZD": "NZ$",
}

_DATE_FIELD_HINTS = ("date", "_at", "day")
_AMOUNT_FIELD_HINTS = (
    "subtotal",
    "total",
    "tax",
    "amount",
    "price",
    "discount",
    "shipping",
    "freight",
)
_QUANTITY_FIELD_HINTS = ("quantity", "qty")
_NON_SCALAR_KEYS = {"document_type"}


@dataclass(frozen=True)
class FieldEvidence:
    field: str
    value: str
    found: bool
    kind: str = "text"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _loose(text: str) -> str:
    """Lowercase, whitespace-collapsed form used for substring search."""
    return re.sub(r"\s+", " ", _fold(text).lower()).strip()


def _alnum(text: str) -> str:
    """Everything that is not a letter or digit removed."""
    return re.sub(r"[^0-9a-z]+", "", _fold(text).lower())


def _decimal_variants(value: object) -> list[str]:
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001 - not a number after all
        return [str(value)]
    variants: list[str] = []
    plain = f"{amount.quantize(Decimal(1))}" if amount == amount.to_integral_value() else format(
        amount.normalize(), "f"
    )
    variants.append(plain)
    try:
        variants.append(f"{Decimal(plain):,.2f}")
    except Exception:  # noqa: BLE001
        # Best effort: a second formatting variant, never fatal.
        pass  # nosec B110
    variants.append(f"{amount:.2f}")
    variants.append(str(value))
    return variants


def _date_variants(value: object) -> list[str]:
    raw = str(value).strip()
    parsed: date | None = None
    for fmt in _DATE_INPUT_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            return [raw]
    month_full = parsed.strftime("%B")
    month_abbr = parsed.strftime("%b")
    return [
        raw,
        parsed.isoformat(),
        f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year}",
        f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year}",
        f"{parsed.day}/{parsed.month}/{parsed.year}",
        f"{parsed.month}/{parsed.day}/{parsed.year}",
        f"{month_full} {parsed.day}, {parsed.year}",
        f"{month_abbr} {parsed.day}, {parsed.year}",
        f"{parsed.day} {month_full} {parsed.year}",
        f"{parsed.day} {month_abbr} {parsed.year}",
        f"{month_full} {parsed.day} {parsed.year}",
        f"{month_abbr} {parsed.day} {parsed.year}",
    ]


def _kind_for(field: str) -> str:
    lowered = field.lower()
    leaf = lowered.rsplit(".", 1)[-1].split("[")[0]
    if any(hint in lowered for hint in _DATE_FIELD_HINTS):
        return "date"
    if any(hint in leaf for hint in _QUANTITY_FIELD_HINTS):
        return "quantity"
    if any(hint in leaf for hint in _AMOUNT_FIELD_HINTS):
        return "amount"
    if "email" in leaf:
        return "email"
    if leaf == "currency":
        return "currency"
    return "text"


def iter_scalar_fields(data: dict | None):
    """Yield ``(field_path, value)`` for every scalar value in the payload."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key in _NON_SCALAR_KEYS or value is None:
            continue
        if key == "line_items":
            for index, item in enumerate(value or []):
                if not isinstance(item, dict):
                    continue
                for item_key, item_value in item.items():
                    if item_value is None:
                        continue
                    yield f"line_items[{index}].{item_key}", item_value
            continue
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if child_value is None:
                    continue
                yield f"{key}.{child_key}", child_value
            continue
        yield key, value


def _candidates(value: object, kind: str) -> list[str]:
    if kind == "date":
        return _date_variants(value)
    if kind in {"amount", "quantity"}:
        return _decimal_variants(value)
    if kind == "currency":
        code = str(value).strip().upper()
        variants = [code]
        symbol = _CURRENCY_SYMBOLS.get(code)
        if symbol:
            variants.append(symbol)
        return variants
    return [str(value)]


def check_evidence(
    data: dict | None,
    source_text: str,
    document_type: str | None = None,  # noqa: ARG001 - kept for a stable signature
) -> list[FieldEvidence]:
    """Return one :class:`FieldEvidence` result per extracted scalar field."""
    loose_haystack = _loose(source_text or "")
    alnum_haystack = _alnum(source_text or "")
    findings: list[FieldEvidence] = []

    for field, value in iter_scalar_fields(data):
        kind = _kind_for(field)
        found = False
        for candidate in _candidates(value, kind):
            if not candidate:
                continue
            loose_candidate = _loose(candidate)
            if loose_candidate and loose_candidate in loose_haystack:
                found = True
                break
            alnum_candidate = _alnum(candidate)
            if len(alnum_candidate) >= 3 and alnum_candidate in alnum_haystack:
                found = True
                break
        findings.append(FieldEvidence(field=field, value=str(value), found=found, kind=kind))

    return findings


def unsupported_fields(findings: list[FieldEvidence]) -> list[FieldEvidence]:
    """Only the fields that could not be located in the source text."""
    return [finding for finding in findings if not finding.found]

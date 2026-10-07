"""Deterministic, rule-based mock provider (the zero-cost default).

It uses regexes only. It NEVER invents a value: if a field cannot be found in
the text, the key is returned as ``None``. The same input always produces the
same output, which is what makes the whole test suite reproducible.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..schemas.document_types import DOCUMENT_SCHEMAS
from ..utils.currency import is_valid_currency
from .provider import AIProvider

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

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CURRENCY_ISO_RE = re.compile(r"\b([A-Z]{3})\b")
_CURRENCY_SYMBOLS = {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP", "\u20b9": "INR", "\u00a5": "JPY"}

_LINE_ITEM_RE = re.compile(
    r"^(?P<description>\S.*?)\s{2,}(?P<quantity>-?\d+(?:\.\d+)?)\s+"
    r"(?P<unit_price>[\d,]+(?:\.\d+)?)\s+(?P<total>[\d,]+(?:\.\d+)?)\s*$"
)

_HEADING_WORDS = (
    "invoice",
    "receipt",
    "purchase order",
    "tax invoice",
    "bill to",
    "ship to",
    "sold to",
    "vendor",
    "supplier",
    "from:",
    "customer",
)

_NUMBER_LABELS = {
    "invoice": ("invoice number", "invoice no", "invoice #", "invoice"),
    "receipt": ("receipt number", "receipt no", "receipt #", "receipt"),
    "purchase_order": ("po number", "po no", "po #", "purchase order number", "purchase order"),
}


def _labeled(text: str, labels: tuple[str, ...]) -> str | None:
    """Return the value after a ``Label:`` line, or None.

    Three patterns, tried in order, so a shorter label can never steal the value
    of a longer one that follows it on the same line (``Receipt Date:`` must
    never be read as the receipt number):

    1. ``Label: value`` / ``Label - value``
    2. ``Label #value``
    3. abbreviated label (``Receipt No. 9002``)
    4. bare fallback (``Invoice INV-2001``) - the value may not contain a colon

    ``[ \\t]`` is used rather than ``\\s`` so a match can never spill onto the
    next line (``\\s`` includes newlines, which would silently join two lines).
    """
    ordered = sorted(labels, key=len, reverse=True)
    alternatives = "|".join(re.escape(label) for label in ordered)
    bare = re.escape(ordered[-1]) if ordered else ""
    patterns = (
        re.compile(rf"(?im)^[ \t]*(?:{alternatives})[ \t]*[:\-][ \t]*(.+?)[ \t]*$"),
        re.compile(rf"(?im)^[ \t]*(?:{alternatives})[ \t]*#[ \t]*(.+?)[ \t]*$"),
        re.compile(rf"(?im)^[ \t]*(?:{alternatives})[ \t]*\.[ \t]*(.+?)[ \t]*$"),
        # "Invoice No INV-1" / "Invoice No. INV-1" without a separator.
        re.compile(rf"(?im)^[ \t]*(?:{alternatives})[ \t]+no[ \t]*[.#]?[ \t]*(.+?)[ \t]*$"),
        re.compile(rf"(?im)^[ \t]*{bare}[ \t]+([^:\n]+?)[ \t]*$"),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value:
                return value
    return None


def _block_line(text: str, label: str) -> str | None:
    """Return the first non-empty line after a ``Label:`` line (addresses, names)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"(?i)^[ \t]*{re.escape(label)}[ \t]*[:\-]?[ \t]*$", line):
            for following in lines[index + 1 : index + 6]:
                candidate = following.strip()
                if candidate:
                    return candidate
        match = re.match(rf"(?i)^[ \t]*{re.escape(label)}[ \t]*[:\-][ \t]*(.+)$", line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


#: A line that looks like a table row (several values separated by wide gaps).
_TABLE_ROW = re.compile(r"\S[ \t]{2,}\S")
#: A line that looks like "Some Label: value".
_LABELLED_LINE = re.compile(r"(?i)^[A-Za-z][A-Za-z0-9 /.'\-]{0,30}[ \t]*[:\-][ \t]*\S")


def _candidate_name(text: str) -> str | None:
    """First plausible company-name line.

    Used only when no ``Vendor:`` style label exists. Headings, table rows,
    ``Label: value`` pairs and lines full of digits are skipped, because
    returning one of those as the vendor name would be inventing a value.
    """
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or len(candidate) > 80:
            continue
        if any(word in candidate.lower() for word in _HEADING_WORDS):
            continue
        if _TABLE_ROW.search(candidate) or _LABELLED_LINE.match(candidate):
            continue
        if len(re.findall(r"\d", candidate)) > 1:
            continue
        return candidate
    return None


def _normalize_date(raw: str | None) -> str | None:
    """Return an ISO-8601 date, or ``None`` when the text is not a real date.

    A value that cannot be parsed - including an impossible one such as
    ``2024-02-30`` - is *missing*, not a date. Passing the raw text through
    would put unvalidated junk into a date field; the business layer then warns
    about the missing date and the document goes to human review.
    """
    if not raw:
        return None
    cleaned = raw.strip().rstrip(".,;")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


#: ``1.234,56`` - dots as thousands separators and a comma as the decimal mark.
_EUROPEAN_AMOUNT = re.compile(r"-?\d{1,3}(?:\.\d{3})+,\d{1,2}")


def _to_amount_str(raw: str | None) -> str | None:
    """Normalize an amount string, or None when it is not numeric.

    Handles ``1,234.56`` (US) and ``1.234,56`` (EU) as well as currency symbols
    and stray text around the number.
    """
    if raw is None:
        return None
    text = raw.strip().replace(" ", "")
    if _EUROPEAN_AMOUNT.fullmatch(text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")  # US thousands separator
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        return str(Decimal(cleaned))
    except InvalidOperation:
        return None


def _labeled_amount(text: str, labels: tuple[str, ...]) -> str | None:
    """Amount after a ``Label:`` line (the whole value is normalized)."""
    return _to_amount_str(_labeled(text, labels))


def _parse_line_items(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        if len(items) >= 200:
            break
        # Skip the table header row.
        if re.match(r"(?i)^\s*(description|item|particulars)\b", line):
            continue
        match = _LINE_ITEM_RE.match(line.rstrip())
        if not match:
            continue
        description = match.group("description").strip(" .|")
        if not description or description.lower() in {"total", "subtotal", "sub total", "tax"}:
            continue
        if re.match(r"(?i)^(sub\s*total|grand\s*total|vat|gst)\b", description):
            continue
        items.append(
            {
                "description": description[:200],
                "quantity": match.group("quantity"),
                "unit_price": _to_amount_str(match.group("unit_price")),
                "total": _to_amount_str(match.group("total")),
            }
        )
    return items


def _email(text: str) -> str | None:
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else None


def _currency(text: str) -> str | None:
    labeled = _labeled(text, ("currency", "currency code"))
    if labeled:
        match = _CURRENCY_ISO_RE.search(labeled.upper())
        if match and is_valid_currency(match.group(1)):
            return match.group(1)
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    # Last resort: an explicit uppercase ISO code that actually appears in the text.
    for token in _CURRENCY_ISO_RE.findall(text):
        if is_valid_currency(token):
            return token
    return None


class MockAIProvider(AIProvider):
    """Rule-based stand-in for a real model. Deterministic and free."""

    name = "mock"

    @property
    def model_name(self) -> str:
        return "mock-rule-based-v1"

    def extract(
        self,
        text: str,
        document_type: str,
        validation_error: str | None = None,
    ) -> dict:
        """Returns a plain dict. Missing fields are ``None`` - never invented.

        ``validation_error`` is accepted for interface parity but ignored: the
        rules are deterministic, so re-running produces the same result.
        """
        text = text or ""
        if document_type not in DOCUMENT_SCHEMAS:
            return {}
        if document_type == "invoice":
            return self._invoice(text)
        if document_type == "receipt":
            return self._receipt(text)
        return self._purchase_order(text)

    # -- per-type rules ----------------------------------------------------
    def _common(self, text: str, vendor_labels: tuple[str, ...]) -> dict:
        vendor_name = _labeled(text, vendor_labels) or _candidate_name(text)
        return {
            "vendor": {"name": vendor_name, "address": None, "email": _email(text)},
            "currency": _currency(text),
            "line_items": _parse_line_items(text),
            "subtotal": _labeled_amount(text, ("subtotal", "sub total", "net amount")),
            "tax": _labeled_amount(text, ("tax", "vat", "gst", "sales tax")),
            "total": _labeled_amount(
                text,
                (
                    "grand total",
                    "total due",
                    "amount due",
                    "total amount",
                    "balance due",
                    "total",
                ),
            ),
        }

    def _invoice(self, text: str) -> dict:
        data = self._common(text, ("vendor", "from", "seller", "supplier", "billed from"))
        data.update(
            {
                "document_type": "invoice",
                "invoice_number": _labeled(text, _NUMBER_LABELS["invoice"]),
                "invoice_date": _normalize_date(
                    _labeled(text, ("invoice date", "date of issue", "issue date", "date"))
                ),
                "due_date": _normalize_date(_labeled(text, ("due date", "payment due", "due"))),
                "customer": {
                    "name": _block_line(text, "Bill To") or _labeled(text, ("customer", "client")),
                    "address": None,
                },
            }
        )
        return data

    def _receipt(self, text: str) -> dict:
        data = self._common(text, ("vendor", "from", "merchant", "store", "seller"))
        data.update(
            {
                "document_type": "receipt",
                "receipt_number": _labeled(text, _NUMBER_LABELS["receipt"]),
                "receipt_date": _normalize_date(
                    _labeled(text, ("receipt date", "transaction date", "date"))
                ),
                "payment_method": _labeled(text, ("payment method", "paid by", "card")),
            }
        )
        return data

    def _purchase_order(self, text: str) -> dict:
        data = self._common(text, ("vendor", "supplier", "seller", "from"))
        data.update(
            {
                "document_type": "purchase_order",
                "po_number": _labeled(text, _NUMBER_LABELS["purchase_order"]),
                "order_date": _normalize_date(_labeled(text, ("order date", "date"))),
                "expected_delivery_date": _normalize_date(
                    _labeled(text, ("expected delivery date", "delivery date", "expected delivery"))
                ),
                "ship_to": {"name": _block_line(text, "Ship To"), "address": None},
            }
        )
        return data

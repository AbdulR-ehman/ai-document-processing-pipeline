"""Quick check of the document schemas."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pydantic import ValidationError  # noqa: E402

from app.schemas.document_types import (  # noqa: E402
    DOCUMENT_SCHEMAS,
    Invoice,
    get_number,
    summarize,
)


def main() -> int:
    inv = Invoice(
        invoice_number="INV-1001",
        invoice_date="2024-03-01",
        vendor={"name": "Acme Supplies Ltd"},
        customer={"name": "Globex"},
        currency="usd",
        line_items=[{"description": "Widget", "quantity": 2, "unit_price": "10.00", "total": "20.00"}],
        subtotal="1,234.56",
        tax="$100.00",
        total=1334.56,
    )
    assert inv.subtotal == Decimal("1234.56"), inv.subtotal
    assert inv.tax == Decimal("100.00"), inv.tax
    assert inv.total == Decimal("1334.56"), inv.total
    assert isinstance(inv.total, Decimal)

    # extra fields rejected
    try:
        Invoice(**{"invoice_number": "X", "owner_id": 99, "role": "admin"})
    except ValidationError as exc:
        assert "extra_forbidden" in str(exc), exc
    else:
        raise AssertionError("extra fields must be rejected")

    # wrong document type rejected
    try:
        DOCUMENT_SCHEMAS["invoice"](document_type="receipt")
    except ValidationError:
        pass
    else:
        raise AssertionError("document_type literal must be enforced")

    # empty string -> None (missing, never fabricated)
    empty = Invoice(invoice_number="   ", vendor={"name": ""})
    assert empty.invoice_number is None and empty.vendor.name is None

    s = summarize(inv.model_dump(mode="json"), "invoice")
    assert s["document_number"] == "INV-1001", s
    assert s["vendor_normalized"] == "acme supplies ltd", s
    assert s["currency"] == "USD", s
    assert s["total_amount"] == "1334.56", s
    assert get_number(inv.model_dump(), "invoice") == "INV-1001"
    print("schemas ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

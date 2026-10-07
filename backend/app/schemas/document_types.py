"""Pydantic schemas for the supported document types.

Design rules enforced here:
  * money uses :class:`decimal.Decimal` (never float);
  * missing information is ``None`` - values are never fabricated;
  * every string has a maximum length and ``line_items`` a maximum count;
  * unknown/extra fields are rejected (``extra="forbid"``), which is the last
    line of defence against prompt-injection payloads that try to add fields.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

MAX_LINE_ITEMS = 200
MAX_MONEY = Decimal("100000000000000")  # 1e14 sanity bound
_CURRENCY_NOISE = re.compile(r"[^0-9eE+\-.]")


def _coerce_decimal(value: Any) -> Any:
    """Best-effort conversion of amounts to ``Decimal`` without losing precision.

    Accepts ints, floats and strings such as ``"1,234.56"`` or ``"$1,234.56"``.
    Anything genuinely non-numeric is left untouched so Pydantic raises a
    schema error instead of us inventing a value.
    """
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "").replace(" ", "")
        if text == "":
            return None
        cleaned = _CURRENCY_NOISE.sub("", text)
        if cleaned in {"", "-", "+", ".", "-.", "+."}:
            return value
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return value
    return value


def _normalize_input(value: Any) -> Any:
    """Recursively trim strings and turn blank strings into ``None``.

    This runs before field validation so that ``""`` means *missing*, never an
    invented empty value. Nested objects and lists (e.g. ``line_items``) are
    handled too.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        return {key: _normalize_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_input(item) for item in value]
    return value


#: Money / numeric amount with up to 4 decimal places.
Amount = Annotated[
    Decimal,
    BeforeValidator(_coerce_decimal),
    Field(max_digits=18, decimal_places=4, ge=-MAX_MONEY, le=MAX_MONEY),
]

#: Quantity with up to 6 decimal places.
Quantity = Annotated[
    Decimal,
    BeforeValidator(_coerce_decimal),
    Field(max_digits=18, decimal_places=6, ge=-MAX_MONEY, le=MAX_MONEY),
]

ShortText = Annotated[str, Field(max_length=200)]
LongText = Annotated[str, Field(max_length=500)]
CodeText = Annotated[str, Field(max_length=64)]


class StrictModel(BaseModel):
    """Base model: no extra fields, trimmed strings, assignment validated."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _pre_normalize(cls, data: Any) -> Any:
        return _normalize_input(data)


class Party(StrictModel):
    name: ShortText | None = None
    address: LongText | None = None
    email: ShortText | None = None


class LineItem(StrictModel):
    description: ShortText | None = None
    quantity: Quantity | None = None
    unit_price: Amount | None = None
    total: Amount | None = None


class Invoice(StrictModel):
    document_type: Literal["invoice"] = "invoice"
    invoice_number: CodeText | None = None
    invoice_date: ShortText | None = None
    due_date: ShortText | None = None
    vendor: Party = Field(default_factory=Party)
    customer: Party = Field(default_factory=Party)
    currency: ShortText | None = None
    line_items: list[LineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: Amount | None = None
    tax: Amount | None = None
    total: Amount | None = None


class Receipt(StrictModel):
    document_type: Literal["receipt"] = "receipt"
    receipt_number: CodeText | None = None
    receipt_date: ShortText | None = None
    vendor: Party = Field(default_factory=Party)
    currency: ShortText | None = None
    payment_method: ShortText | None = None
    line_items: list[LineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: Amount | None = None
    tax: Amount | None = None
    total: Amount | None = None


class PurchaseOrder(StrictModel):
    document_type: Literal["purchase_order"] = "purchase_order"
    po_number: CodeText | None = None
    order_date: ShortText | None = None
    expected_delivery_date: ShortText | None = None
    vendor: Party = Field(default_factory=Party)
    ship_to: Party = Field(default_factory=Party)
    currency: ShortText | None = None
    line_items: list[LineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: Amount | None = None
    tax: Amount | None = None
    total: Amount | None = None


DOCUMENT_SCHEMAS: dict[str, type[StrictModel]] = {
    "invoice": Invoice,
    "receipt": Receipt,
    "purchase_order": PurchaseOrder,
}

#: Field holding the primary document number, per type.
NUMBER_FIELD = {
    "invoice": "invoice_number",
    "receipt": "receipt_number",
    "purchase_order": "po_number",
}

#: Field holding the primary document date, per type.
DATE_FIELD = {
    "invoice": "invoice_date",
    "receipt": "receipt_date",
    "purchase_order": "order_date",
}

#: Field holding the secondary/derived date, per type.
SECONDARY_DATE_FIELD = {
    "invoice": "due_date",
    "receipt": None,
    "purchase_order": "expected_delivery_date",
}


def get_schema(document_type: str | None) -> type[StrictModel] | None:
    return DOCUMENT_SCHEMAS.get(document_type or "")


def get_number(data: dict | None, document_type: str | None) -> str | None:
    return _get(data, NUMBER_FIELD.get(document_type or ""))


def get_primary_date(data: dict | None, document_type: str | None) -> str | None:
    return _get(data, DATE_FIELD.get(document_type or ""))


def get_vendor_name(data: dict | None) -> str | None:
    vendor = (data or {}).get("vendor")
    if isinstance(vendor, dict):
        name = vendor.get("name")
        return name if isinstance(name, str) and name.strip() else None
    if isinstance(vendor, str) and vendor.strip():
        return vendor
    return None


def _get(data: dict | None, field_name: str | None) -> str | None:
    if not data or not field_name:
        return None
    value = data.get(field_name)
    return value if isinstance(value, str) and value.strip() else None


def summarize(data: dict, document_type: str | None) -> dict:
    """Flatten an extracted payload into the columns kept on ``documents``."""
    from ..utils.helpers import normalize_name

    vendor_name = get_vendor_name(data)
    total = data.get("total")
    currency = data.get("currency")
    return {
        "document_number": get_number(data, document_type),
        "document_date": get_primary_date(data, document_type),
        "vendor_name": vendor_name,
        "vendor_normalized": normalize_name(vendor_name) or None,
        "total_amount": None if total is None else str(total),
        "currency": (currency.upper() if isinstance(currency, str) and currency else None),
    }

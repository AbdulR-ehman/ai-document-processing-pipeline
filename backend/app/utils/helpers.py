"""Small helpers shared across the app."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """New opaque identifier (used for documents and correlation ids)."""
    return str(uuid.uuid4())


def new_request_id() -> str:
    """Short correlation id used in logs and error responses."""
    return uuid.uuid4().hex[:16]


def strip_control_characters(value: str) -> str:
    """Remove control characters (including NUL) from a string."""
    return _CONTROL_CHARS.sub("", value)


def normalize_whitespace(value: str) -> str:
    """Collapse runs of whitespace and trim."""
    return re.sub(r"[ \t]+", " ", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def normalize_name(value: str | None) -> str:
    """Normalize a vendor/customer name for comparisons.

    Lower-cases, removes accents, strips punctuation and collapses whitespace.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(value: str, limit: int) -> str:
    """Truncate a string to ``limit`` characters."""
    if value is None:
        return value
    return value if len(value) <= limit else value[:limit]

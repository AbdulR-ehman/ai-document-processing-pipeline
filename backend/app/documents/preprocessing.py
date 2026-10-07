"""Text preprocessing: normalize raw extracted text before it reaches the AI."""

from __future__ import annotations

import re

_MULTI_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def preprocess_text(text: str, max_chars: int | None = None) -> str:
    """Normalize whitespace and strip control characters.

    Deliberately conservative: it never drops lines or rewrites content, so the
    evidence check can still find every value in the preprocessed text.
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL.sub("", normalized)
    normalized = _TRAILING_SPACES.sub("\n", normalized)
    normalized = _MULTI_BLANK_LINES.sub("\n\n", normalized)
    normalized = normalized.strip()
    if max_chars and len(normalized) > max_chars:
        normalized = normalized[:max_chars]
    return normalized

"""Upload validation.

Nothing the client sends is trusted: not the filename, not the extension, not
the declared ``Content-Type``. The final decision is made from the *bytes*
(magic-byte sniffing) plus a strict allowlist, and a few well-known attack
patterns are rejected outright.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..utils.helpers import strip_control_characters

#: extension -> internal kind
ALLOWED_EXTENSIONS: dict[str, str] = {".pdf": "pdf", ".txt": "txt", ".md": "md"}

#: kind -> accepted declared MIME types
MIME_ALLOWED: dict[str, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "txt": frozenset({"text/plain"}),
    "md": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
}

#: Declared types that are ignored (browsers use these constantly). The content
#: sniff is the real check in that case.
NEUTRAL_MIMES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "*/*"}
)

#: Content types written to the database per kind.
DETECTED_CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
}

#: Extensions that must never appear anywhere in an uploaded filename.
DANGEROUS_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe", ".dll", ".com", ".bat", ".cmd", ".msi", ".scr", ".pif", ".cpl",
        ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".ps1", ".psm1", ".jar",
        ".sh", ".bash", ".zsh", ".run", ".bin", ".app", ".dmg", ".pkg", ".deb",
        ".rpm", ".apk", ".py", ".pyc", ".php", ".asp", ".aspx", ".jsp", ".html",
        ".htm", ".svg", ".lnk", ".reg", ".hta", ".iso", ".img", ".zip", ".gz",
        ".tar", ".rar", ".7z",
    }
)

MAX_FILENAME_LENGTH = 200
MAX_ORIGINAL_FILENAME_LENGTH = 255

#: Characters kept when producing the sanitized display/stored name.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._()\-]")

_MAGIC_SIGNATURES = {
    b"%PDF-": "pdf",
    b"PK\x03\x04": "zip",
    b"\x7fELF": "elf",
    b"MZ": "exe",
    b"\xd0\xcf\x11\xe0": "ole",
    b"Rar!\x1a\x07": "rar",
    b"\x1f\x8b": "gzip",
    b"7z\xbc\xaf": "7z",
}


class UploadValidationError(ValueError):
    """Raised when an upload fails validation. ``code`` is safe to log/return."""

    def __init__(self, code: str, message: str, status_code: int = 415) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ValidatedFile:
    original_filename: str
    safe_filename: str
    extension: str
    kind: str
    detected_content_type: str
    size: int


def sanitize_filename(name: str) -> str:
    """Produce a safe, human-readable filename for display/storage metadata."""
    cleaned = strip_control_characters(name).replace("\\", "/")
    cleaned = cleaned.rsplit("/", 1)[-1]
    cleaned = _UNSAFE_NAME_CHARS.sub("_", cleaned).strip(" .")
    if not cleaned:
        cleaned = "document"
    stem, dot, ext = cleaned.rpartition(".")
    if not dot:
        stem, ext = cleaned, ""
    stem = stem[: MAX_FILENAME_LENGTH - len(ext) - 1] or "document"
    return f"{stem}.{ext}" if ext else stem


def validate_original_filename(name: str | None) -> str:
    """Reject traversal, absolute paths, NUL bytes and odd names."""
    if not name or not isinstance(name, str) or not name.strip():
        raise UploadValidationError("invalid_filename", "A file name is required.", 400)
    if "\x00" in name:
        raise UploadValidationError("null_byte", "The file name contains a null byte.", 400)
    if len(name) > MAX_ORIGINAL_FILENAME_LENGTH:
        raise UploadValidationError("filename_too_long", "The file name is too long.", 400)
    if any(ord(ch) < 32 for ch in name):
        raise UploadValidationError(
            "control_characters", "The file name contains control characters.", 400
        )

    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UploadValidationError("path_traversal", "Absolute paths are not allowed.", 400)
    if "/" in normalized:
        raise UploadValidationError("path_traversal", "Only a plain file name is allowed.", 400)
    if ".." in normalized:
        raise UploadValidationError("path_traversal", "Path traversal is not allowed.", 400)
    base = normalized
    if base in {".", ".."} or base.startswith("."):
        raise UploadValidationError(
            "invalid_filename", "Hidden or reserved file names are not allowed.", 400
        )
    return base


def validate_extension(filename: str) -> tuple[str, str]:
    """Return ``(extension, kind)`` or raise for a disallowed extension."""
    lowered = filename.lower()
    for dangerous in DANGEROUS_EXTENSIONS:
        if lowered.endswith(dangerous) or f"{dangerous}." in lowered:
            raise UploadValidationError(
                "dangerous_extension",
                "Files that look executable or archived are not accepted.",
                415,
            )
    _, dot, ext = lowered.rpartition(".")
    if not dot:
        raise UploadValidationError(
            "unsupported_extension",
            "Only PDF, TXT and Markdown files are supported.",
            415,
        )
    suffix = f".{ext}"
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(
            "unsupported_extension",
            "Only PDF, TXT and Markdown files are supported.",
            415,
        )
    return suffix, ALLOWED_EXTENSIONS[suffix]


def validate_declared_mime(kind: str, declared: str | None) -> None:
    """Check the client-supplied Content-Type, ignoring neutral values."""
    declared = (declared or "").split(";")[0].strip().lower()
    if declared in NEUTRAL_MIMES:
        return
    if declared not in MIME_ALLOWED[kind]:
        raise UploadValidationError(
            "mime_mismatch",
            "The declared content type does not match the file extension.",
            415,
        )


def sniff_content(probe: bytes, kind: str) -> None:
    """Verify the actual bytes. Never trust the extension alone."""
    if not probe:
        raise UploadValidationError("empty_file", "The uploaded file is empty.", 400)

    looked_like = None
    for signature, name in _MAGIC_SIGNATURES.items():
        if probe.startswith(signature):
            looked_like = name
            break

    if kind == "pdf":
        if not probe.startswith(b"%PDF-"):
            raise UploadValidationError(
                "content_mismatch", "The file is not a valid PDF (bad signature).", 415
            )
        return

    # Text kinds: must look like text, not like a binary container.
    if looked_like is not None:
        raise UploadValidationError(
            "content_mismatch",
            "The file content does not match a plain-text document.",
            415,
        )
    if b"\x00" in probe:
        raise UploadValidationError("content_mismatch", "The file contains null bytes.", 415)


def validate_upload(
    filename: str | None,
    declared_content_type: str | None,
    probe: bytes,
    size: int,
    max_bytes: int,
) -> ValidatedFile:
    """Full upload validation. Returns metadata or raises ``UploadValidationError``."""
    base = validate_original_filename(filename)
    extension, kind = validate_extension(base)

    if size <= 0:
        raise UploadValidationError("empty_file", "The uploaded file is empty.", 400)
    if size > max_bytes:
        raise UploadValidationError(
            "too_large",
            f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.",
            413,
        )

    validate_declared_mime(kind, declared_content_type)
    sniff_content(probe, kind)

    return ValidatedFile(
        original_filename=base,
        safe_filename=sanitize_filename(base),
        extension=extension,
        kind=kind,
        detected_content_type=DETECTED_CONTENT_TYPES[kind],
        size=size,
    )

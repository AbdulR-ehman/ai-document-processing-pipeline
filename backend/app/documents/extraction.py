"""Text extraction from PDF, TXT and Markdown files.

Every failure mode returns a structured status instead of raising, so one bad
document can never take the request (or the app) down:

  ok | empty | scanned_no_text | encrypted | corrupted | too_many_pages
  | timeout | unsupported

PDF handling is defensive: page count is capped *before* extraction, encrypted
documents are detected, and PDF JavaScript / embedded links / attachments are
never followed or executed (pypdf only ever reads the text layer).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path


class TextExtractionStatus:
    OK = "ok"
    EMPTY = "empty"
    SCANNED_NO_TEXT = "scanned_no_text"
    ENCRYPTED = "encrypted"
    CORRUPTED = "corrupted"
    TOO_MANY_PAGES = "too_many_pages"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


_SAFE_MESSAGES = {
    TextExtractionStatus.EMPTY: "The file is empty.",
    TextExtractionStatus.SCANNED_NO_TEXT: "No text layer found (it may be a scanned image).",
    TextExtractionStatus.ENCRYPTED: "The PDF is password protected and cannot be read.",
    TextExtractionStatus.CORRUPTED: "The file is corrupted or unreadable.",
    TextExtractionStatus.TOO_MANY_PAGES: "The PDF has too many pages to process.",
    TextExtractionStatus.TIMEOUT: "Text extraction took too long and was stopped.",
    TextExtractionStatus.UNSUPPORTED: "This file type is not supported.",
}


@dataclass
class ExtractionResult:
    text: str = ""
    status: str = TextExtractionStatus.OK
    page_count: int | None = None
    truncated: bool = False
    metadata: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == TextExtractionStatus.OK

    @property
    def user_message(self) -> str | None:
        return _SAFE_MESSAGES.get(self.status)


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes as text, trying a few encodings. Never raises."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _strip_markdown(text: str) -> str:
    """Light cleanup so the AI sees prose rather than markup."""
    import re

    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # code fences
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", text)  # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)  # blockquotes
    text = re.sub(r"(\*\*|__|\*|_)(.+?)\1", r"\2", text)  # emphasis
    return text


def _extract_pdf(path: Path, max_pages: int) -> ExtractionResult:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:  # noqa: BLE001 - any pypdf failure means "corrupted"
        return ExtractionResult(status=TextExtractionStatus.CORRUPTED, errors=[type(exc).__name__])

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                return ExtractionResult(status=TextExtractionStatus.ENCRYPTED)
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot read"
            return ExtractionResult(
                status=TextExtractionStatus.ENCRYPTED, errors=[type(exc).__name__]
            )

    try:
        page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(status=TextExtractionStatus.CORRUPTED, errors=[type(exc).__name__])

    if page_count > max_pages:
        return ExtractionResult(
            status=TextExtractionStatus.TOO_MANY_PAGES,
            page_count=page_count,
            metadata={"max_pages": max_pages},
        )
    if page_count == 0:
        return ExtractionResult(status=TextExtractionStatus.EMPTY, page_count=0)

    chunks: list[str] = []
    failures = 0
    for index in range(page_count):
        try:
            chunks.append(reader.pages[index].extract_text() or "")
        except Exception:  # noqa: BLE001 - a single bad page must not kill the run
            failures += 1
            chunks.append("")

    text = "\n".join(chunks)
    result = ExtractionResult(
        text=text,
        page_count=page_count,
        metadata={"pages_failed": failures},
    )
    if failures == page_count:
        result.status = TextExtractionStatus.CORRUPTED
    elif not text.strip():
        result.status = TextExtractionStatus.SCANNED_NO_TEXT
    elif failures:
        result.errors.append(f"{failures} page(s) could not be read")
    return result


def _extract_text_file(path: Path, kind: str) -> ExtractionResult:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ExtractionResult(status=TextExtractionStatus.CORRUPTED, errors=[type(exc).__name__])

    if not data:
        return ExtractionResult(status=TextExtractionStatus.EMPTY)

    text, encoding = decode_text(data)
    if kind == "md":
        text = _strip_markdown(text)
    result = ExtractionResult(text=text, metadata={"encoding": encoding})
    if not text.strip():
        result.status = TextExtractionStatus.EMPTY
    return result


def extract_text(
    path: Path,
    kind: str,
    max_pages: int = 50,
    max_chars: int = 100_000,
    timeout_seconds: float = 20.0,
) -> ExtractionResult:
    """Extract text from a validated file, with a hard wall-clock timeout."""
    if kind not in {"pdf", "txt", "md"}:
        return ExtractionResult(status=TextExtractionStatus.UNSUPPORTED)

    box: dict[str, ExtractionResult] = {}

    def _work() -> None:
        box["result"] = (
            _extract_pdf(path, max_pages) if kind == "pdf" else _extract_text_file(path, kind)
        )

    worker = threading.Thread(target=_work, name="text-extraction", daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        # NOTE: a Python thread cannot be killed, but the request returns
        # immediately and the thread is a daemon, so it cannot block shutdown.
        # Together with MAX_PDF_PAGES this bounds the damage a hostile PDF can do.
        return ExtractionResult(status=TextExtractionStatus.TIMEOUT, errors=["extraction timeout"])

    result = box.get("result") or ExtractionResult(
        status=TextExtractionStatus.CORRUPTED, errors=["extraction produced no result"]
    )
    if result.status == TextExtractionStatus.OK and max_chars and len(result.text) > max_chars:
        result.text = result.text[:max_chars]
        result.truncated = True
    return result

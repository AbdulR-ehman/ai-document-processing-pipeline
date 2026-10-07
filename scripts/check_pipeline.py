"""End-to-end pipeline check on real sample files (no HTTP layer)."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

os.environ["DATABASE_URL"] = "sqlite:///./_pipeline_check.db"
os.environ["UPLOAD_DIR"] = "./_pipeline_uploads"

from app.ai.provider import get_provider  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory, init_db, reset_engine  # noqa: E402
from app.repositories import user_repo  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.schemas.document_types import get_number  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402
from app.services.uploads import store_upload  # noqa: E402

SAMPLES = PROJECT_ROOT / "documents" / "samples"


class FakeUpload:
    def __init__(self, name: str, content_type: str, data: bytes) -> None:
        self.filename = name
        self.content_type = content_type
        self.file = io.BytesIO(data)


CASES = [
    ("invoice_valid.txt", "text/plain", "COMPLETED"),
    ("invoice_valid.txt", "text/plain", "DUPLICATE"),
    ("receipt_valid.txt", "text/plain", "COMPLETED"),
    ("purchase_order_valid.txt", "text/plain", "COMPLETED"),
    ("invoice_valid.md", "text/markdown", "COMPLETED"),
    ("invoice_valid.pdf", "application/pdf", "DUPLICATE"),
    ("invoice_wrong_totals.txt", "text/plain", "NEEDS_REVIEW"),
    ("injection_invoice.txt", "text/plain", "COMPLETED"),
    ("scanned_no_text.pdf", "application/pdf", "EXTRACTION_FAILED"),
    ("corrupted.pdf", "application/pdf", "EXTRACTION_FAILED"),
    ("encrypted.pdf", "application/pdf", "EXTRACTION_FAILED"),
]


def main() -> int:
    reset_engine()
    get_settings.cache_clear()
    settings = get_settings()
    init_db()
    session = get_session_factory()()
    user = user_repo.create_user(session, "check@example.com", hash_password("Passw0rdTest1"))
    session.commit()
    provider = get_provider(settings)

    failures = 0
    for name, content_type, expected in CASES:
        data = (SAMPLES / name).read_bytes()
        document = store_upload(
            session, user, FakeUpload(name, content_type, data), settings
        )
        session.commit()
        result = run_pipeline(session, document, provider, settings)
        ok = result.status == expected
        failures += 0 if ok else 1
        print(
            f"{'OK ' if ok else 'FAIL'} {name:28s} -> {result.status:18s} (expected {expected})"
        )
        if not ok:
            for stage in result.stages:
                print("      ", stage)
            for error in result.errors:
                print("       error:", error)
        if name in {"invoice_wrong_totals.txt", "injection_invoice.txt", "invoice_valid.txt"}:
            total = get_number(result.extracted or {}, document.document_type)
            print("       number:", total, "| dup:", document.duplicate_status)
            print("       keys:", sorted((result.extracted or {}).keys()))

    session.close()
    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

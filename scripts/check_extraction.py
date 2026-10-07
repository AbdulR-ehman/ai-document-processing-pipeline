"""Development check for extraction + mock provider + evidence (Phases 3-4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.ai.mock_provider import MockAIProvider  # noqa: E402
from app.documents.classifier import classify  # noqa: E402
from app.documents.evidence import check_evidence, unsupported_fields  # noqa: E402
from app.documents.extraction import extract_text  # noqa: E402
from app.documents.preprocessing import preprocess_text  # noqa: E402
from app.schemas.document_types import DOCUMENT_SCHEMAS  # noqa: E402

SAMPLES = PROJECT_ROOT / "documents" / "samples"


def run(name: str, kind: str) -> None:
    result = extract_text(SAMPLES / name, kind, max_pages=50, max_chars=100_000, timeout_seconds=10)
    print(f"\n=== {name} [{kind}] status={result.status} pages={result.page_count} chars={len(result.text)}")
    if not result.ok:
        print("   message:", result.user_message)
        return
    text = preprocess_text(result.text, 100_000)
    classification = classify(text)
    doc_type = classification.document_type or "invoice"
    print("   classified:", classification.document_type, classification.confidence)
    data = MockAIProvider().extract(text, doc_type)
    model = DOCUMENT_SCHEMAS[doc_type](**data)
    payload = model.model_dump(mode="json")
    print("   extracted:", json.dumps(payload, default=str)[:600])
    findings = check_evidence(payload, text, doc_type)
    missing = unsupported_fields(findings)
    print(f"   evidence: {len(findings)} fields, {len(missing)} unsupported")
    for item in missing:
        print("      NOT FOUND:", item.field, "=", item.value)


def main() -> int:
    run("invoice_valid.txt", "txt")
    run("invoice_valid.pdf", "pdf")
    run("receipt_valid.txt", "txt")
    run("purchase_order_valid.txt", "txt")
    run("invoice_wrong_totals.txt", "txt")
    run("invoice_valid.md", "md")
    run("scanned_no_text.pdf", "pdf")
    run("corrupted.pdf", "pdf")
    run("encrypted.pdf", "pdf")
    run("injection_invoice.txt", "txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

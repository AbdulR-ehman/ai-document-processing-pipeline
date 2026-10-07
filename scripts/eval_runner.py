"""Reusable evaluation runner shared by ``check_eval.py`` and the pytest gate.

Runs the whole pipeline over every ``*.txt`` / ``*.md`` document in the
evaluation corpus and compares the extracted fields with the ``.expected.json``
file written next to it. Returns a plain dict so callers can print a table, write
a report or assert thresholds.
"""

from __future__ import annotations

import io
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = PROJECT_ROOT / "documents" / "eval"
CONTENT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf"}


class FakeUpload:
    """Minimal stand-in for ``fastapi.UploadFile``."""

    def __init__(self, name: str, content_type: str, data: bytes) -> None:
        self.filename = name
        self.content_type = content_type
        self.file = io.BytesIO(data)


def flatten(value, prefix: str = "") -> dict:
    """Flatten a nested payload into dotted paths (``vendor.name``)."""
    out: dict = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def comparable(value) -> str | None:
    """Normalize a value so ``11`` and ``11.00`` compare equal."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return format(Decimal(text), "f")
    except InvalidOperation:
        return " ".join(text.lower().split())


def fields_match(expected, actual) -> bool:
    return comparable(expected) == comparable(actual)


def load_cases(eval_dir: Path = EVAL_DIR) -> list[tuple[Path, dict]]:
    cases: list[tuple[Path, dict]] = []
    if not eval_dir.is_dir():
        return cases
    for path in sorted(eval_dir.iterdir()):
        if path.suffix not in CONTENT_TYPES:
            continue
        expected_file = eval_dir / f"{path.stem}.expected.json"
        if expected_file.is_file():
            cases.append((path, json.loads(expected_file.read_text(encoding="utf-8"))))
    return cases


def evaluate(session, user, provider, settings, eval_dir: Path = EVAL_DIR) -> dict:
    """Run every corpus document through the pipeline and score the results."""
    from app.services.pipeline import run_pipeline
    from app.services.uploads import store_upload

    rows: list[dict] = []
    fields_total = fields_correct = 0
    documents_passed = 0
    cases = load_cases(eval_dir)

    for path, expected in cases:
        document = store_upload(
            session,
            user,
            FakeUpload(path.name, CONTENT_TYPES[path.suffix], path.read_bytes()),
            settings,
        )
        session.commit()
        result = run_pipeline(session, document, provider, settings)
        actual_fields = flatten(result.extracted or {})

        mismatches = []
        for field, want in expected.get("fields", {}).items():
            fields_total += 1
            got = actual_fields.get(field)
            if fields_match(want, got):
                fields_correct += 1
            else:
                mismatches.append(f"{field}: want {want!r} got {got!r}")

        status_ok = expected.get("expected_status") in (None, result.status)
        type_ok = expected.get("document_type") in (None, document.document_type)
        passed = status_ok and type_ok and not mismatches
        documents_passed += int(passed)

        rows.append(
            {
                "document": path.name,
                "expected_status": expected.get("expected_status"),
                "actual_status": result.status,
                "expected_type": expected.get("document_type"),
                "actual_type": document.document_type,
                "fields_expected": len(expected.get("fields", {})),
                "fields_correct": len(expected.get("fields", {})) - len(mismatches),
                "mismatches": mismatches,
                "passed": passed,
            }
        )

    accuracy = (fields_correct / fields_total) if fields_total else 0.0
    return {
        "provider": provider.name,
        "documents": rows,
        "documents_passed": documents_passed,
        "documents_total": len(cases),
        "field_accuracy": round(accuracy, 4),
        "fields_correct": fields_correct,
        "fields_total": fields_total,
    }


def make_session_and_provider(user_email: str = "eval@example.com"):
    """Build a throwaway session, owner user, provider and settings."""
    from app.ai.provider import get_provider
    from app.auth.passwords import hash_password
    from app.config import get_settings
    from app.db import get_session_factory, init_db
    from app.repositories import user_repo

    settings = get_settings()
    init_db(settings)
    session = get_session_factory()()
    user = user_repo.create_user(session, user_email, hash_password("Passw0rdEval1"))
    session.commit()
    return session, user, get_provider(settings), settings
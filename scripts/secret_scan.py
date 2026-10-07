"""Repeatable secret scan for the working tree.

Looks for the patterns that actually leak credentials in a Python/JS project:
private keys, cloud and vendor API keys, JWTs, and any assignment that binds a
literal to a secret-looking name. Test fixtures and the example environment file
are allowed explicitly, so the scan stays useful instead of noisy.

Run:  .venv\\Scripts\\python.exe scripts\\secret_scan.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules", "uploads"}
SKIP_FILES = {
    # Fixtures and documentation are not credentials.
    "backend/tests/conftest.py",
    "backend/tests/test_auth.py",
    "backend/tests/test_security.py",
    "scripts/check_api.py",
    "scripts/check_pipeline.py",
    "scripts/check_eval.py",
    "scripts/smoke.py",
    ".env.example",
    "requirements-freeze.txt",
    "documents/eval/report.json",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".html", ".css", ".md", ".txt", ".json", ".cfg", ".ini",
    ".toml", ".yml", ".yaml", ".env", ".example",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("json web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b")),
    (
        "secret assigned a literal",
        re.compile(
            r"(?i)\b(secret_key|api_key|apikey|password|passwd|token)\s*[:=]\s*"
            r"[\"'][^\"'\n]{12,}[\"']"
        ),
    ),
]


def iter_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.as_posix() in SKIP_FILES:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".env"}:
            continue
        yield relative, path


def main() -> int:
    findings: list[str] = []
    scanned = 0
    for relative, path in iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file
            continue
        scanned += 1
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: {label}")

    # A committed .env would be a leak on its own.
    if (PROJECT_ROOT / ".env").is_file():
        findings.append(".env: real environment file present (must stay untracked)")

    print(f"scanned {scanned} text files")
    if findings:
        print(f"potential secrets: {len(findings)}")
        for item in findings:
            print(f"  - {item}")
        return 1
    print("no secrets found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Shared pytest fixtures.

Environment is configured *before* any ``app.*`` import so the settings cache
picks it up. Every test gets a brand-new SQLite database, upload directory and
rate-limit state via :func:`build_client`, so tests are order-independent and
can run in any combination.

Run:  .venv\\Scripts\\python.exe -m pytest backend\\tests -q
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
SAMPLES = PROJECT_ROOT / "documents" / "samples"

# --- process-wide test configuration (set before importing the app) ---------
TEST_DB = PROJECT_ROOT / "_pytest.db"
TEST_UPLOADS = PROJECT_ROOT / "_pytest_uploads"
os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "pytest-only-secret-key-0123456789abcdefghijklmnop",
        "DATABASE_URL": f"sqlite:///{TEST_DB}",
        "UPLOAD_DIR": str(TEST_UPLOADS),
        "AI_PROVIDER": "mock",
        "MAX_UPLOAD_MB": "1",
        # Generous limits: dedicated tests exercise the small ones themselves.
        "RATE_LIMIT_API": "100000/minute",
        "RATE_LIMIT_LOGIN": "100000/minute",
        "RATE_LIMIT_UPLOAD": "100000/minute",
        "RATE_LIMIT_REPROCESS": "100000/minute",
        "RATE_LIMIT_EXPORT": "100000/minute",
        "LOGIN_MAX_FAILED": "5",
        "LOGIN_LOCKOUT_SECONDS": "60",
        "EXPOSE_DOCS": "true",
        "LOG_LEVEL": "WARNING",
        # The admin role is granted only to this address (never "first user wins").
        "ADMIN_EMAIL": "admin@example.com",
    }
)

_UNSET = object()
BASE = "/api/v1"


@dataclass
class Account:
    """A registered test account with ready-to-use headers."""

    email: str
    password: str
    token: str
    role: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def _wipe_database() -> None:
    if TEST_DB.exists():
        TEST_DB.unlink()
    shutil.rmtree(TEST_UPLOADS, ignore_errors=True)


@contextmanager
def build_client(overrides: dict | None = None):
    """Yield a fresh TestClient with a clean database and limit state."""
    from app.auth.ratelimit import login_throttle, rate_limiter
    from app.config import get_settings
    from app.db import reset_engine
    from app.main import create_app

    overrides = dict(overrides or {})
    saved = {key: os.environ.get(key, _UNSET) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        reset_engine()
        get_settings.cache_clear()
        rate_limiter.reset()
        login_throttle.clear()
        _wipe_database()
        application = create_app(get_settings())
        from fastapi.testclient import TestClient

        with TestClient(application) as test_client:
            yield test_client
    finally:
        reset_engine()
        get_settings.cache_clear()
        rate_limiter.reset()
        login_throttle.clear()
        for key, original in saved.items():
            if original is _UNSET:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


# ---------------------------------------------------------------------------
# Reusable fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    with build_client() as test_client:
        yield test_client


#: Credentials for the seeded administrator (mirrors the seed script).
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Adm1nPassw0rd"


@pytest.fixture()
def admin(client):
    """An administrator, created the only way the app allows: ``ensure_admin``.

    The HTTP ``/auth/register`` endpoint can never mint a role, so the fixture
    goes through the same code path as ``scripts/seed_admin.py`` and then logs
    in to obtain a token.
    """
    from app.db import get_session_factory
    from app.services.admin import ensure_admin

    session = get_session_factory()()
    try:
        ensure_admin(session, ADMIN_EMAIL, ADMIN_PASSWORD)
    finally:
        session.close()
    return login_account(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture()
def user(client, admin):
    """An ordinary account, created through the public registration endpoint."""
    return register_account(client, "user@example.com", "Us3rPassw0rd!")


def login_account(client, email: str, password: str) -> Account:
    response = client.post(f"{BASE}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return Account(
        email=email, password=password, token=body["access_token"], role=body["user"]["role"]
    )


def register_account(client, email: str, password: str) -> Account:
    """Register through the public endpoint - always yields ``role=user``."""
    response = client.post(
        f"{BASE}/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Account(
        email=email, password=password, token=body["access_token"], role=body["user"]["role"]
    )


def upload_sample(
    client,
    headers: dict,
    name: str = "invoice_valid.txt",
    content_type: str = "text/plain",
    data: bytes | None = None,
):
    """POST a sample file (or explicit bytes) to the upload endpoint."""
    payload = data if data is not None else (SAMPLES / name).read_bytes()
    return client.post(
        f"{BASE}/documents/upload",
        headers=headers,
        files={"file": (name, payload, content_type)},
    )


def set_document_status(document_id: str, status: str) -> None:
    """Force a lifecycle status directly (for edge-case state setup only)."""
    from app.db import session_scope
    from app.models import Document

    with session_scope() as session:
        document = session.get(Document, document_id)
        assert document is not None
        document.status = status


def get_detail(client, headers: dict, document_id: str) -> dict:
    response = client.get(f"{BASE}/documents/{document_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["document"]

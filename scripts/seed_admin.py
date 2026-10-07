"""Create (or promote) the administrator account named by ``ADMIN_EMAIL``.

Usage::

    $env:ADMIN_EMAIL = "you@example.com"
    .venv\Scripts\python.exe scripts\seed_admin.py

The password is always prompted for twice with hidden input - it is never read
from the environment, never stored in plain text and never logged. Only its
argon2id hash is written to the database. ``POST /auth/register`` can never
produce an admin account, which is why this script exists.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.repositories import audit_repo  # noqa: E402
from app.services.admin import AdminSetupError, ensure_admin  # noqa: E402


def read_password() -> str:
    """Prompt twice (hidden) and require both entries to match."""
    first = getpass.getpass("Password for the administrator: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    return first


def main() -> int:
    settings = get_settings()
    if not settings.has_admin_email:
        print("ADMIN_EMAIL is not set. Add it to your environment or .env first.", file=sys.stderr)
        return 1

    init_db(settings)
    try:
        password = read_password()
        with session_scope(settings) as session:
            user, action = ensure_admin(session, settings.admin_email, password)
            audit_repo.log_action(
                session,
                actor_user_id=user.id,
                action="register",
                detail={"seed": action},
            )
            session.commit()
    except AdminSetupError as exc:
        print(f"Cannot set up the administrator: {exc}", file=sys.stderr)
        return 1

    print(f"Administrator {user.email} ({action}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
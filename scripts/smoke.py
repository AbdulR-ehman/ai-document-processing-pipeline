"""Ad-hoc smoke checks used during development (not part of the test suite)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_smoke.db")


def main() -> int:
    from app.config import get_settings, validate_startup
    from app.db import init_db, session_scope

    settings = get_settings()
    validate_startup(settings)
    init_db()
    with session_scope() as session:
        from app.models import User

        session.add(User(email="smoke@example.com", password_hash="x"))
    with session_scope() as session:
        from app.models import User

        users = session.query(User).all()
        assert len(users) == 1, users
        print("smoke ok:", settings.app_env, settings.ai_provider, users[0].email)
    db_file = settings.database_path
    from app.db import reset_engine

    reset_engine()
    if db_file and db_file.exists():
        db_file.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Liveness/readiness probe (no auth, no secrets, rate-limit exempt)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...utils.helpers import utcnow
from ..deps import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session: Session = Depends(get_db)) -> Response:
    """200 with ``db: ok`` when the app can reach its database."""
    from fastapi.responses import JSONResponse

    try:
        session.execute(text("SELECT 1"))
        database = "ok"
        status_code = 200
    except Exception:  # noqa: BLE001 - report, never leak, the driver error
        database = "unavailable"
        status_code = 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "time": utcnow().isoformat(),
        },
    )

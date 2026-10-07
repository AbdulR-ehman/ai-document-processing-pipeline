"""FastAPI application factory.

Middleware is registered innermost-first (Starlette: the *last* middleware
added is the outermost one), so the effective order for every request is::

    request context (id + logging + security headers)
      -> CORS
        -> global rate limit + request-size cap
          -> routes / exception handlers

Every response - including errors - carries ``X-Request-ID``, which is the same
correlation id written to the logs.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.deps import client_ip
from .api.errors import error_payload, register_exception_handlers
from .api.routers import auth, documents, health, reports, review
from .auth.ratelimit import login_throttle, rate_limiter
from .config import Settings, get_settings, validate_startup
from .db import init_db
from .utils.helpers import new_request_id
from .utils.logging_setup import configure_logging, get_logger

API_PREFIX = "/api/v1"
#: Paths that skip the global rate limit (probes run often).
UNMETERED_PATHS = {f"{API_PREFIX}/health"}
#: Swagger/ReDoc need a different CSP than the JSON API.
DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
DOCS_CSP = (
    "default-src 'self' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "font-src 'self' data: https://cdn.jsdelivr.net"
)

logger = get_logger("app.http")


def _is_valid_request_id(value: str) -> bool:
    return 0 < len(value) <= 36 and all(ch.isalnum() or ch in "-_" for ch in value)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    validate_startup(settings)  # fail fast on dangerous configuration
    # Wire lockout settings into the process-wide throttle (env-driven).
    login_throttle.configure(
        max_failures=settings.login_max_failed,
        lockout_seconds=settings.login_lockout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(settings)
        logger.info(
            "startup complete (env=%s provider=%s docs=%s)",
            settings.app_env,
            settings.ai_provider,
            settings.expose_docs,
        )
        yield

    app = FastAPI(
        title="AI Document Processing Pipeline",
        summary="Upload -> extract -> validate -> duplicate-check -> review -> export.",
        version="1.0.0",
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url="/redoc" if settings.expose_docs else None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    # --- innermost: global rate limit + payload size cap --------------------
    @app.middleware("http")
    async def _limits(request: Request, call_next):
        path = request.url.path
        if path.startswith(API_PREFIX) and path not in UNMETERED_PATHS:
            raw_length = request.headers.get("content-length", "")
            if raw_length.isdigit() and int(raw_length) > settings.max_upload_bytes:
                return JSONResponse(
                    status_code=413,
                    content=error_payload(
                        request,
                        "too_large",
                        f"Request body exceeds the {settings.max_upload_mb} MB limit.",
                        413,
                    ),
                )
            allowed, retry_after = rate_limiter.check(
                f"api:{client_ip(request)}", settings.rate_limit_api
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content=error_payload(
                        request,
                        "rate_limited",
                        "Too many requests. Please slow down and try again.",
                        429,
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
        return await call_next(request)

    # --- CORS (outside the rate limiter so preflights are answered) ---------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,  # Bearer tokens, never cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Content-Disposition", "Retry-After"],
        max_age=600,
    )

    # --- outermost: correlation id, logging, security headers ---------------
    @app.middleware("http")
    async def _context(request: Request, call_next):
        incoming = (request.headers.get("x-request-id") or "").strip()
        request_id = incoming if _is_valid_request_id(incoming) else new_request_id()
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last resort; handlers cover normal cases
            logger.exception(
                "unhandled error on %s %s",
                request.method,
                request.url.path,
                extra={"request_id": request_id},
            )
            response = JSONResponse(
                status_code=500,
                content=error_payload(
                    request,
                    "internal_error",
                    "An unexpected error occurred. Please try again.",
                    500,
                ),
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        headers = response.headers
        headers["X-Request-ID"] = request_id
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Content-Security-Policy"] = (
            DOCS_CSP if request.url.path in DOCS_PATHS else API_CSP
        )
        logger.info(
            "%s %s -> %s (%sms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={"request_id": request_id},
        )
        return response

    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(documents.router, prefix=API_PREFIX)
    app.include_router(review.router, prefix=API_PREFIX)
    app.include_router(reports.router, prefix=API_PREFIX)
    app.include_router(health.router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "name": "AI Document Processing Pipeline",
            "api": API_PREFIX,
            "health": f"{API_PREFIX}/health",
            "docs": "/docs" if settings.expose_docs else None,
        }

    return app


app = create_app()

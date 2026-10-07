"""Uniform error responses and exception handlers.

Every error body has the same shape::

    {"error": {"code": "not_found", "message": "...", "status": 404,
               "request_id": "1a2b3c4d5e6f7a8b"}}

Messages are written by us (never echo internal details, stack traces, SQL or
client-supplied values), and ``request_id`` is the correlation id found in the
server logs.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

#: Status code -> short machine-readable code for framework-raised errors.
_REASON_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
}

_DEFAULT_MESSAGE = {
    401: "Authentication is required.",
    403: "You do not have permission to perform this action.",
    404: "The requested resource was not found.",
    405: "That method is not allowed on this resource.",
    413: "The request payload is too large.",
    415: "That content type is not supported.",
    422: "The request could not be processed as sent.",
    429: "Too many requests. Please slow down and try again.",
    500: "An unexpected error occurred. Please try again.",
}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def error_payload(request: Request, code: str, message: str, status: int) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status,
            "request_id": request_id_of(request),
        }
    }


def api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict | None = None,
) -> HTTPException:
    """Raise an HTTP exception that keeps its machine-readable code."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = exc.status_code
        detail = exc.detail
        if isinstance(detail, dict) and "message" in detail:
            code = str(detail.get("code") or _REASON_BY_STATUS.get(status, "error"))
            message = str(detail["message"])
        elif isinstance(detail, str) and detail and status < 500:
            # Hand-written messages from lower layers are already safe (no
            # internals), but anything else falls back to a default phrase.
            code = _REASON_BY_STATUS.get(status, "error")
            message = detail
        else:
            code = _REASON_BY_STATUS.get(status, "error")
            message = _DEFAULT_MESSAGE.get(status, "The request could not be processed.")
        return JSONResponse(
            status_code=status,
            content=error_payload(request, code, message, status),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Only field *locations* and error types are returned - never the raw
        # input values, which could echo back secrets or file contents.
        fields = []
        for error in exc.errors()[:10]:
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            fields.append({"field": location or "<body>", "issue": error.get("type", "invalid")})
        message = "One or more fields are invalid."
        if fields:
            message = f"{message} " + "; ".join(f"{f['field']}: {f['issue']}" for f in fields)
        return JSONResponse(
            status_code=422,
            content=error_payload(request, "validation_error", message[:500], 422),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error on %s %s",
            request.method,
            request.url.path,
            extra={"request_id": request_id_of(request)},
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                request, "internal_error", _DEFAULT_MESSAGE[500], 500
            ),
        )

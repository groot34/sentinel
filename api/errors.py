"""Error handling and sanitization utilities for Sentinel 2.0 API Layer."""

from __future__ import annotations

import re
from typing import Any
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.persistence import ConcurrencyError, PersistenceError

_PATH_REGEX = re.compile(r"([A-Za-z]:[\\/]|/)[^\s:\"']+")


def sanitize_error_message(message: str) -> str:
    """Sanitize error messages by removing file paths and truncating length."""
    if not isinstance(message, str):
        message = str(message)

    # Strip local filesystem paths
    sanitized = _PATH_REGEX.sub("[path_redacted]", message)

    # Strip newline / carriage returns to keep logs & payloads clean
    sanitized = sanitized.replace("\n", " ").replace("\r", " ").strip()

    # Truncate to 200 characters max
    if len(sanitized) > 200:
        sanitized = sanitized[:197] + "..."

    return sanitized


def error_json_response(status_code: int, error_code: str, detail: str) -> JSONResponse:
    """Build a standard JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error_code,
            "detail": sanitize_error_message(detail),
        },
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle Starlette HTTPExceptions with standard format."""
    code_map = {
        400: "bad_request",
        404: "not_found",
        409: "conflict",
        500: "internal_error",
        503: "service_unavailable",
    }
    error_code = code_map.get(exc.status_code, "error")
    detail = str(exc.detail) if exc.detail else "An HTTP error occurred."
    return error_json_response(exc.status_code, error_code, detail)


async def concurrency_exception_handler(request: Request, exc: ConcurrencyError) -> JSONResponse:
    """Map ConcurrencyError to 409 Conflict."""
    return error_json_response(409, "conflict", str(exc))


async def persistence_exception_handler(request: Request, exc: PersistenceError) -> JSONResponse:
    """Map PersistenceError to 503 Service Unavailable."""
    return error_json_response(503, "persistence_unavailable", str(exc))


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map unhandled exceptions to sanitized 500 Internal Server Error."""
    return error_json_response(500, "internal_error", "An internal server error occurred.")

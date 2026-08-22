"""Error taxonomy and HTTP problem responses.

Every error carries a stable machine-readable ``code``.  Later phases add codes
rather than inventing new response shapes — risk rejections, order-validation
failures and broker errors all become members of this taxonomy.

Responses follow RFC 9457 ``application/problem+json``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id, get_logger

logger = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    """Base class for all deliberately raised application errors."""

    code: str = "internal_error"
    status_code: int = 500
    title: str = "Internal error"

    def __init__(self, detail: str = "", **context: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.context = context


class ConfigurationInvalidError(AppError):
    code = "configuration_invalid"
    status_code = 500
    title = "Configuration invalid"


class DependencyUnavailableError(AppError):
    code = "dependency_unavailable"
    status_code = 503
    title = "Dependency unavailable"


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    title = "Not found"


class ValidationFailedError(AppError):
    code = "validation_failed"
    status_code = 422
    title = "Validation failed"


class OperationNotPermittedError(AppError):
    """The request is well-formed but forbidden by a safety rule.

    Reserved for the safety controls in later phases (kill switch engaged,
    trading mode does not permit the action, risk gate rejected the trade).
    """

    code = "operation_not_permitted"
    status_code = 409
    title = "Operation not permitted"


def _problem(status: int, code: str, title: str, detail: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"about:blank#{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "correlation_id": get_correlation_id(),
    }
    body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log("application_error", extra={"code": exc.code, "detail": exc.detail})
        return _problem(exc.status_code, exc.code, exc.title, exc.detail, **exc.context)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(
            422,
            "validation_failed",
            "Validation failed",
            "The request payload failed validation.",
            errors=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            exc.status_code,
            "http_error",
            "HTTP error",
            str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", extra={"error_type": type(exc).__name__})
        # Never leak internals to the client.
        return _problem(500, "internal_error", "Internal error", "An unexpected error occurred.")

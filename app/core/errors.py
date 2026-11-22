"""Shared error types and handlers."""

import logging
import re
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class UnimplementedError(NotImplementedError):
    """Raised by integration points whose owning module has not been implemented yet.

    Never catch and ignore this: an unimplemented ledger or notification call must abort the
    surrounding transaction rather than let a state change commit without its audit entry or
    reviewer notification.
    """

    def __init__(self, feature: str, requirement: str, owner_module: str) -> None:
        self.feature = feature
        self.requirement = requirement
        self.owner_module = owner_module
        super().__init__(
            f"{feature} is not implemented yet ({requirement}; owner: app/modules/{owner_module})"
        )


class ErrorCode(StrEnum):
    """Machine-readable error codes (Phase 5 contract delta \u03947; docs/spec-decisions.md D-29).

    Sent as `error_code` next to `detail`, with `message` repeating the text, for the errors the
    delta names. Other errors keep the plain `{"detail"}` body (B-22).
    """

    MALFORMED_BODY = "MALFORMED_BODY"
    MALFORMED_QUERY = "MALFORMED_QUERY"
    RECORD_ERASED = "RECORD_ERASED"
    NOT_SUBMITTABLE = "NOT_SUBMITTABLE"
    REPORT_NOT_CURRENT = "REPORT_NOT_CURRENT"
    REPORT_HASH_MISMATCH = "REPORT_HASH_MISMATCH"
    REPORT_ALREADY_SUBMITTED = "REPORT_ALREADY_SUBMITTED"
    RETENTION_NOT_EXPIRED = "RETENTION_NOT_EXPIRED"
    ALREADY_ERASED = "ALREADY_ERASED"
    NO_DISPOSITION = "NO_DISPOSITION"


class CodedHTTPException(StarletteHTTPException):
    """An HTTP error that carries an ErrorCode."""

    def __init__(
        self,
        status_code: int,
        error_code: ErrorCode,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code, message, headers)
        self.error_code = error_code


def error_body(detail: Any, error_code: ErrorCode | None = None, message: str | None = None):
    """B-22 body, plus `error_code` and `message` when the error has a code (\u03947)."""
    body: dict[str, Any] = {"detail": detail}
    if error_code is not None:
        body["error_code"] = error_code.value
        body["message"] = message if message is not None else str(detail)
    return body


def http_error(status_code: int, exc: Exception) -> StarletteHTTPException:
    """An HTTP error for a domain exception, coded when the exception has an `error_code`."""
    code = getattr(exc, "error_code", None)
    if code is not None:
        return CodedHTTPException(status_code, ErrorCode(code), str(exc))
    return StarletteHTTPException(status_code, str(exc))


_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _allowed_methods(request: Request) -> str:
    """Every method the API documents for this path. Starlette's 405 names only the first
    route's methods, which is wrong when GET and POST are separate routes on one path (\u03945).
    Taken from the app's own OpenAPI document, which lists every mounted operation."""
    path = request.url.path
    methods: set[str] = set()
    for template, operations in request.app.openapi().get("paths", {}).items():
        pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path):
            methods |= {m.upper() for m in operations if m in _HTTP_METHODS}
    return ", ".join(sorted(methods))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(UnimplementedError)
    async def _unimplemented(_: Request, exc: UnimplementedError) -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": str(exc)})

    # Phase 2 Section 5: malformed or invalid input is 400 Bad Request (FastAPI defaults to 422).
    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type")}
            for err in exc.errors()
        ]
        in_query = any(e["loc"][:1] == ["query"] for e in errors)
        code = ErrorCode.MALFORMED_QUERY if in_query else ErrorCode.MALFORMED_BODY
        message = "the query is malformed" if in_query else "the request is malformed"
        return JSONResponse(status_code=400, content=error_body(errors, code, message))

    # Every HTTP error, including 404 for an unknown route and 405, is {"detail": <str>} (B-22).
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = exc.error_code if isinstance(exc, CodedHTTPException) else None
        headers = exc.headers
        if exc.status_code == 405:
            headers = {**(headers or {}), "Allow": _allowed_methods(request)}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.detail, code),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled exception", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

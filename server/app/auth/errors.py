"""Error envelope `{error: {code, message, details?}}` (contracts/NOTES.md, D12).

`register_error_handlers(app)` installs app-wide handlers so every non-2xx JSON response —
including framework 404/405 and request validation — uses the envelope.
TODO(T2.3): these handlers are app-wide; move to a shared module (e.g. app/api/errors.py)
once that package exists. Kept here because T2.2's write scope is app/auth/.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


class UnauthenticatedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            401,
            "unauthenticated",
            "Missing or invalid session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class AppleTokenInvalidError(AppError):
    """`details.reason`: signature | expired | issuer | audience | nonce_mismatch |
    authorization_code.

    TODO(contract): `authorization_code` (Apple rejected the code, or the id_token from the
    code exchange is missing/unverifiable/for another subject) is not yet in NOTES.md.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            401,
            "apple_token_invalid",
            "Apple identity token failed verification.",
            details={"reason": reason},
        )


class TimezoneInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "timezone_invalid",
            "X-Timezone header is required and must be an IANA timezone name.",
        )


class AppleUnavailableError(AppError):
    # TODO(contract): `apple_unavailable` is not in the NOTES.md code table (codes are open
    # strings, and these routes fall under `default: InternalError`). Main session to ratify.
    def __init__(self) -> None:
        super().__init__(503, "apple_unavailable", "Sign in with Apple is temporarily unavailable.")


class AppleRevocationFailedError(AppError):
    """Account deletion blocked: a stored Apple credential cannot be revoked.

    TODO(contract): not yet in the NOTES.md code table. `details.reason`:
    `undecryptable_credential` (with `details.key_id`, the cipher's primary key id), or
    `apple_error` (credentials kept changing across revocation passes).
    """

    def __init__(self, reason: str, *, key_id: int | None = None) -> None:
        details: dict[str, Any] = {"reason": reason}
        if key_id is not None:
            details["key_id"] = key_id
        super().__init__(
            503,
            "apple_revocation_failed",
            "Your Sign in with Apple authorization could not be revoked, so the account "
            "was not deleted. Please try again later.",
            details=details,
        )


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    404: "not_found",
    405: "method_not_allowed",
}


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details),
        headers=exc.headers,
    )


def _loc_to_path(loc: tuple[Any, ...] | list[Any]) -> str:
    parts = list(loc)
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    return ".".join(str(p) for p in parts)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        return JSONResponse(
            status_code=400, content=error_body("bad_request", "Request body is not valid JSON.")
        )
    fields = [{"path": _loc_to_path(e.get("loc", ())), "message": e.get("msg", "")} for e in errors]
    return JSONResponse(
        status_code=422,
        content=error_body("validation_failed", "Request body is invalid.", {"fields": fields}),
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "internal_error" if exc.status_code >= 500 else "error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message),
        headers=getattr(exc, "headers", None),
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log type and route only: exception messages may carry request data (tokens).
    logger.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return JSONResponse(
        status_code=500, content=error_body("internal_error", "An unexpected error occurred.")
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

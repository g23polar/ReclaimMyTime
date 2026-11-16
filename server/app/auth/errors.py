"""Auth error types. The envelope and its app-wide handlers live in app/errors.py (moved
in T2.3); they are re-exported here so existing `app.auth.errors` imports keep working.
"""

from __future__ import annotations

from typing import Any

from app.errors import AppError, error_body, register_error_handlers

__all__ = [
    "AppError",
    "AppleNotConfiguredError",
    "AppleRevocationFailedError",
    "AppleTokenInvalidError",
    "AppleUnavailableError",
    "TimezoneInvalidError",
    "UnauthenticatedError",
    "error_body",
    "register_error_handlers",
]


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
    """503 `apple_unavailable`: Apple unreachable (T2.2), or Sign in with Apple is not
    configured on this server (T6.0, D26)."""

    def __init__(self, message: str = "Sign in with Apple is temporarily unavailable.") -> None:
        super().__init__(503, "apple_unavailable", message)


class AppleNotConfiguredError(AppleUnavailableError):
    """No APPLE_BUNDLE_ID: Sign in with Apple is not offered (T6.0, D26)."""

    def __init__(self) -> None:
        super().__init__("Sign in with Apple is not available on this server.")


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

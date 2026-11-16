"""Google Calendar errors.

HTTP-facing (`AppError`): `gcal_not_connected` (404), `gcal_revocation_failed` (503), and
the POST /gcal/complete codes `gcal_state_invalid` (400), `gcal_scope_insufficient` (422),
`gcal_exchange_failed` (502) (contracts/NOTES.md, T4.1 + review 2026-11-15). Domain errors raised by
`GcalService.access_token` for T4.2/T4.3 callers are plain exceptions; those callers decide
how to surface them.
"""

from __future__ import annotations

from typing import Any

from app.errors import AppError

# Redirect codes for GET /gcal/callback (contracts/NOTES.md), exactly these three.
ACCESS_DENIED = "gcal_access_denied"
STATE_INVALID = "gcal_state_invalid"
EXCHANGE_FAILED = "gcal_exchange_failed"


class GcalStateInvalidError(AppError):
    """Undecryptable, malformed, expired, or issued to a different user. Deliberately one
    undifferentiated answer (no oracle for which check failed)."""

    def __init__(self) -> None:
        super().__init__(
            400,
            STATE_INVALID,
            "This Google connection attempt is invalid or has expired. Please start again.",
        )


class GcalScopeInsufficientError(AppError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "gcal_scope_insufficient",
            "Calendar access was not granted. Please allow both calendar permissions.",
        )


class GcalExchangeFailedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            502,
            EXCHANGE_FAILED,
            "Google did not complete the connection. Please try again.",
        )


class GcalNotConnectedError(AppError):
    def __init__(self) -> None:
        super().__init__(404, "gcal_not_connected", "Google Calendar is not connected.")


class GcalRevocationFailedError(AppError):
    """The Google grant could not be revoked, so nothing was changed (fail closed, like
    Apple). `details.reason`: `undecryptable_credential` (with `details.key_id`, the stored
    row's key id) | `google_error` (5xx, timeout, network, unexpected answer, or the
    credential kept changing across revocation passes)."""

    def __init__(self, reason: str, *, key_id: int | None = None) -> None:
        details: dict[str, Any] = {"reason": reason}
        if key_id is not None:
            details["key_id"] = key_id
        super().__init__(
            503,
            "gcal_revocation_failed",
            "Your Google Calendar authorization could not be revoked, so nothing was "
            "changed. Please try again later.",
            details=details,
        )


# ------------------------------------------------------------------ access-token minting


class GcalError(Exception):
    """Base for domain errors from the gcal service."""


class GcalConnectionInactiveError(GcalError):
    """No connection row, or its status is already `revoked`: the user must reconnect."""


class GcalGrantRevokedError(GcalConnectionInactiveError):
    """Google answered `invalid_grant` just now; the row was marked `revoked`."""


class GcalCredentialUndecryptableError(GcalError):
    def __init__(self, key_id: int) -> None:
        super().__init__(f"google refresh token undecryptable (key_id={key_id})")
        self.key_id = key_id


class GcalTransientError(GcalError):
    """Google unreachable / 5xx / unexpected answer. Connection status left untouched."""

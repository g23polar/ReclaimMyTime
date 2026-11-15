"""T2.1 write-path exceptions → the contract envelope (contracts/NOTES.md error table).

Installed as app-wide exception handlers by `app.api.install`. Raising through the request
transaction dependency rolls it back first, so nothing a failed write touched is committed.
`ChangesetStale` is handled in the confirm route (it must stage a fresh proposal in a new
transaction); the handler here is only a fallback.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db.write_paths import (
    BlockNotFound,
    BlockNotStarted,
    BlockSuperseded,
    ChangesetNotFound,
    ChangesetNotPending,
    ChangesetStale,
    InvalidMutation,
    PlanVerificationError,
    PreferencesNotSet,
    WritePathError,
)
from app.errors import AppError, error_body

logger = logging.getLogger(__name__)

# InvalidMutation.code → HTTP status. Documented codes first; the *_exists codes are not in
# the NOTES table. TODO(contract): `project_exists` / `exception_exists` / `pin_exists` are
# undocumented (server-assigned ids make them unreachable from REST; reachable only through
# agent-staged lists) — 409 assumed.
INVALID_MUTATION_STATUS: dict[str, int] = {
    "not_found": 404,
    "project_archived": 409,
    "validation_failed": 422,
    "project_exists": 409,
    "exception_exists": 409,
    "pin_exists": 409,
}

_STATUS_MESSAGES = {
    "confirmed": "This changeset was already confirmed.",
    "rejected": "This changeset was already rejected.",
    "expired": "This changeset expired: a newer proposal replaced it.",
}


def invalid_mutation_error(exc: InvalidMutation) -> AppError:
    status = INVALID_MUTATION_STATUS.get(exc.code, 422)
    if status == 422:
        # TODO(contract): semantic failures carry no field path; `path` is "" for those.
        fields = [{"path": "", "message": exc.message}]
        return AppError(422, "validation_failed", exc.message, {"fields": fields})
    return AppError(status, exc.code, exc.message)


def write_path_error(exc: Exception) -> AppError:
    """Map a T2.1 exception to its AppError (PlanVerificationError excluded: see handler)."""
    if isinstance(exc, InvalidMutation):
        return invalid_mutation_error(exc)
    if isinstance(exc, PreferencesNotSet):
        return AppError(404, "preferences_not_set", "Preferences have not been set yet.")
    if isinstance(exc, ChangesetNotFound):
        return _not_found("changeset", str(exc))
    if isinstance(exc, BlockNotFound):
        return _not_found("block", str(exc))
    if isinstance(exc, ChangesetNotPending):
        message = _STATUS_MESSAGES.get(exc.status, f"This changeset is {exc.status}.")
        return AppError(409, "changeset_not_pending", message, {"status": exc.status})
    if isinstance(exc, ChangesetStale):
        return AppError(409, "changeset_stale", "This proposal is out of date.")
    if isinstance(exc, BlockSuperseded):
        return AppError(409, "block_superseded", "This block is not in the current live plan.")
    if isinstance(exc, BlockNotStarted):
        return AppError(
            409, "block_not_started", "Only blocks that have started can be completed or skipped."
        )
    return AppError(500, "internal_error", "An unexpected error occurred.")


def _not_found(resource: str, ident: str) -> AppError:
    return AppError(
        404,
        "not_found",
        f"{resource.capitalize()} not found.",
        {"resource": resource, "id": ident},
    )


def _respond(err: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=err.status_code, content=error_body(err.code, err.message, err.details)
    )


async def _write_path_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, PlanVerificationError):
        # A solver bug, not a user error. Log violation kinds only — never user data.
        logger.error(
            "plan verification failed on %s %s: kinds=%s",
            request.method,
            request.url.path,
            sorted({v.kind for v in exc.violations}),
        )
        return _respond(AppError(500, "internal_error", "An unexpected error occurred."))
    return _respond(write_path_error(exc))


def register_write_path_handlers(app: FastAPI) -> None:
    for exc_type in (WritePathError, InvalidMutation, PreferencesNotSet):
        app.add_exception_handler(exc_type, _write_path_handler)

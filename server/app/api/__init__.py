"""Scheduling REST API (T2.3): projects, preferences, exceptions, pins, blocks, plans,
changesets — per contracts/openapi.yaml — plus the thin gcal connection routes (T4.1; logic
in app.gcal) and the Google sign-in routes (T6.0; logic in app.auth.google_signin). The other
auth routes live in app.auth; the agent route (M3) is not here."""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    """Mount the routers and the T2.1 write-path error handlers on `app`."""
    from app.api import (
        blocks,
        changesets,
        exceptions,
        gcal,
        google_login,
        pins,
        plans,
        preferences,
        projects,
    )
    from app.api.errors import register_write_path_handlers

    register_write_path_handlers(app)
    for module in (
        projects,
        preferences,
        exceptions,
        pins,
        blocks,
        plans,
        changesets,
        gcal,
        google_login,
    ):
        app.include_router(module.router)

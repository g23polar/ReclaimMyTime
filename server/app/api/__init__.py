"""Scheduling REST API (T2.3): projects, preferences, exceptions, pins, blocks, plans,
changesets — per contracts/openapi.yaml. Auth routes live in app.auth; the agent route (M3)
and gcal routes (M4) are not here."""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    """Mount the routers and the T2.1 write-path error handlers on `app`."""
    from app.api import blocks, changesets, exceptions, pins, plans, preferences, projects
    from app.api.errors import register_write_path_handlers

    register_write_path_handlers(app)
    for module in (projects, preferences, exceptions, pins, blocks, plans, changesets):
        app.include_router(module.router)

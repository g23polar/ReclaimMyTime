"""The per-process write-back registry (T4.3a) on `app.state.gcal_outbox`, and the confirm kick.

The registry resolves the session maker, clock and gcal runtime from `app.state` at drain
time (tests swap them per test); the gcal runtime is only built for a user who has a
connection row, so a user who never connected Google costs one read and needs no Google
configuration.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI

from app.api.deps import clock_for_app, maker_for_app
from app.api.gcal import runtime_for_app
from app.gcal.writeback import DrainResult, OutboxRegistry, WriteBack

logger = logging.getLogger(__name__)


def get_outbox(app: FastAPI) -> OutboxRegistry:
    registry: OutboxRegistry | None = getattr(app.state, "gcal_outbox", None)
    if registry is None:

        async def drain(user_id: uuid.UUID) -> DrainResult:
            writer = WriteBack(maker_for_app(app), lambda: runtime_for_app(app), clock_for_app(app))
            return await writer.drain(user_id)

        registry = app.state.gcal_outbox = OutboxRegistry(drain)
    return registry


def kick_outbox(app: FastAPI, user_id: uuid.UUID) -> None:
    """Fire-and-forget drain after a commit. Never blocks or fails the caller's response."""
    try:
        get_outbox(app).kick(user_id)
    except Exception as exc:
        logger.error("gcal write-back kick failed: %s", type(exc).__name__)

"""The one staging path for REST writes (contracts D2/D3): build manual `StagedMutation`s,
carry the pending list forward when the write targets a project it is creating (O9), fetch
calendar busy time (T4.2; before the lock, see app.api.busy), solve via `propose_changeset`,
and return the 202 Changeset body."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.busy import BusyWindow, ensure_covers, fetch_window
from app.api.deps import transaction
from app.api.views import changeset_view
from app.db.models.staged import StagedMutation, parse_staged
from app.db.write_paths import (
    carry_forward,
    preview_carry_forward,
    propose_changeset,
    solve_horizon,
)
from app.gcal.busy import BusyProvider


def manual(kind: str, payload: dict[str, Any]) -> StagedMutation:
    """A user-initiated REST mutation (`source: manual`, no agent interpretation)."""
    return parse_staged(
        [{"kind": kind, "source": "manual", "interpretation": None, "payload": payload}]
    )[0]


Build = Callable[[AsyncSession], Awaitable["list[StagedMutation] | JSONResponse"]]


async def stage_request(
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    build: Build,
    *,
    now: datetime,
    busy: BusyProvider,
) -> JSONResponse:
    """The staged-write request flow (T4.2 + review round 1), in three steps so that no pooled
    connection is held across the Google call and no Google call happens under the lock:

    1. a short transaction runs `build` (validation → the mutations, or a direct JSONResponse
       such as the name-only PATCH) and previews the solve horizon; it is closed before
    2. busy time is fetched (the provider uses its own short sessions);
    3. the write transaction runs `build` again (authoritative), takes the per-user lock,
       checks the busy window still covers the solve (`ensure_covers`) and stages.

    `build` must be safe to run twice; any ids it mints are regenerated, which is harmless."""
    async with transaction(maker) as tx:
        built = await build(tx)
        if isinstance(built, JSONResponse):
            return built
        preview = await preview_carry_forward(tx, user_id, built)
        horizon = await solve_horizon(tx, user_id, preview, now)
    window = await fetch_window(busy, user_id, horizon)
    async with transaction(maker) as tx:
        built = await build(tx)
        if isinstance(built, JSONResponse):
            return built
        return accepted(await stage_with_busy(tx, user_id, built, now=now, window=window))


async def stage_with_busy(
    tx: AsyncSession,
    user_id: uuid.UUID,
    mutations: list[StagedMutation],
    *,
    now: datetime,
    window: BusyWindow,
) -> dict[str, Any]:
    """Stage `mutations` against busy time fetched earlier (see `stage_request`)."""
    staged = await carry_forward(tx, user_id, mutations)  # takes the per-user lock
    ensure_covers(window, await solve_horizon(tx, user_id, staged, now))
    changeset_id = await propose_changeset(
        tx, user_id, staged, now=now, busy=window.intervals, source="manual"
    )
    return await changeset_view(tx, user_id, changeset_id)


def accepted(body: dict[str, Any]) -> JSONResponse:
    """D3: staged writes answer 202 + Changeset."""
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

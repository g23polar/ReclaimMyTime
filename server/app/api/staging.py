"""The one staging path for REST writes (contracts D2/D3): build manual `StagedMutation`s,
carry the pending list forward when the write targets a project it is creating (O9), solve
via `propose_changeset`, and return the 202 Changeset body."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.views import changeset_view
from app.db.models.staged import StagedMutation, parse_staged
from app.db.write_paths import carry_forward, propose_changeset


def manual(kind: str, payload: dict[str, Any]) -> StagedMutation:
    """A user-initiated REST mutation (`source: manual`, no agent interpretation)."""
    return parse_staged(
        [{"kind": kind, "source": "manual", "interpretation": None, "payload": payload}]
    )[0]


async def stage(
    tx: AsyncSession, user_id: uuid.UUID, mutations: list[StagedMutation], *, now: datetime
) -> dict[str, Any]:
    staged = await carry_forward(tx, user_id, mutations)
    # TODO(T4.2): pass freeBusy intervals; every REST solve runs with busy=[] until then.
    changeset_id = await propose_changeset(tx, user_id, staged, now=now, source="manual")
    return await changeset_view(tx, user_id, changeset_id)


def accepted(body: dict[str, Any]) -> JSONResponse:
    """D3: staged writes answer 202 + Changeset."""
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

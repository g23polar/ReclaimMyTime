"""Preferences (`preferences` tag): GET the committed row; PUT stages a full replacement."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.busy import Busy
from app.api.deps import Maker, Now, RawBody, Tx, UserId, parse_body
from app.api.mappers import preferences_out
from app.api.schemas import PreferencesRequest
from app.api.staging import manual, stage_request
from app.auth.dependencies import require_timezone
from app.db.models import UserPreferences
from app.db.models.staged import StagedMutation
from app.db.write_paths import sweep_past_blocks
from app.errors import AppError

router = APIRouter(tags=["preferences"], dependencies=[Depends(require_timezone)])

_PREFS: TypeAdapter[PreferencesRequest] = TypeAdapter(PreferencesRequest)


@router.get("/preferences", operation_id="getPreferences")
async def get_preferences(user_id: UserId, tx: Tx, now: Now) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    row = (
        await tx.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise AppError(404, "preferences_not_set", "Preferences have not been set yet.")
    return preferences_out(row)


@router.put("/preferences", operation_id="putPreferences", status_code=202)
async def put_preferences(
    user_id: UserId, maker: Maker, now: Now, busy: Busy, body: RawBody
) -> JSONResponse:
    prefs = parse_body(_PREFS, body)
    mutation = manual("preferences_updated", {"preferences": prefs.model_dump(mode="json")})

    async def build(tx: AsyncSession) -> list[StagedMutation]:
        return [mutation]

    return await stage_request(maker, user_id, build, now=now, busy=busy)

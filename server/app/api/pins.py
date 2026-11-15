"""Pins (`pins` tag): list committed pins/forbids; DELETE stages `pin_removed`. No POST (D16):
pins come only from block edit/delete or the agent's stage_pin."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.deps import Now, Tx, UserId, not_found
from app.api.mappers import pin_out
from app.api.staging import accepted, manual, stage
from app.auth.dependencies import require_timezone
from app.db.models import BlockPin
from app.db.write_paths import sweep_past_blocks

router = APIRouter(tags=["pins"], dependencies=[Depends(require_timezone)])


@router.get("/pins", operation_id="listPins")
async def list_pins(
    user_id: UserId, tx: Tx, now: Now, include_past: bool = Query(False)
) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    query = select(BlockPin).where(BlockPin.user_id == user_id)
    if not include_past:
        query = query.where(func.upper(BlockPin.span) > now)
    query = query.order_by(func.lower(BlockPin.span), BlockPin.id)
    return {"pins": [pin_out(p) for p in (await tx.execute(query)).scalars()]}


@router.delete("/pins/{pin_id}", operation_id="deletePin", status_code=202)
async def delete_pin(user_id: UserId, tx: Tx, now: Now, pin_id: uuid.UUID) -> JSONResponse:
    found = (
        await tx.execute(
            select(BlockPin.id).where(BlockPin.id == pin_id, BlockPin.user_id == user_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise not_found("pin", pin_id)
    mutation = manual("pin_removed", {"pin_id": str(pin_id)})
    return accepted(await stage(tx, user_id, [mutation], now=now))

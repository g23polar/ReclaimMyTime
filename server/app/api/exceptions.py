"""Availability exceptions (`exceptions` tag): list/get committed rows; create/delete staged."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    DeviceTz,
    Now,
    RawBody,
    Tx,
    UserId,
    not_found,
    parse_body,
    validation_failed,
)
from app.api.mappers import exception_out, ts
from app.api.schemas import ExceptionDraft
from app.api.staging import accepted, manual, stage
from app.auth.dependencies import require_timezone
from app.db.models import AvailabilityException
from app.db.models.staged import hhmm_to_minute
from app.db.solver_io import local_to_utc
from app.db.write_paths import sweep_past_blocks

router = APIRouter(tags=["exceptions"], dependencies=[Depends(require_timezone)])

_DRAFT: TypeAdapter[ExceptionDraft] = TypeAdapter(ExceptionDraft)


async def _get(
    tx: AsyncSession, user_id: uuid.UUID, exception_id: uuid.UUID
) -> AvailabilityException | None:
    return (
        await tx.execute(
            select(AvailabilityException).where(
                AvailabilityException.id == exception_id,
                AvailabilityException.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


@router.get("/exceptions", operation_id="listExceptions")
async def list_exceptions(
    user_id: UserId, tx: Tx, now: Now, include_past: bool = Query(False)
) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    query = select(AvailabilityException).where(AvailabilityException.user_id == user_id)
    if not include_past:
        query = query.where(func.upper(AvailabilityException.span) > now)
    query = query.order_by(func.lower(AvailabilityException.span), AvailabilityException.id)
    return {"exceptions": [exception_out(e) for e in (await tx.execute(query)).scalars()]}


@router.post("/exceptions", operation_id="createException", status_code=202)
async def create_exception(
    user_id: UserId, tx: Tx, now: Now, tz: DeviceTz, body: RawBody
) -> JSONResponse:
    draft = parse_body(_DRAFT, body)
    if not draft.blocks_all and not draft.blocks_tags:
        raise validation_failed(
            [{"path": "blocks_tags", "message": "must be non-empty unless blocks_all is true"}],
            "Set blocks_all or at least one blocks_tags value.",
        )
    # D10: resolved once, here, with X-Timezone. Omitted start_time = 00:00; omitted end_time
    # = end of end_date (next local midnight).
    start = local_to_utc(draft.start_date, hhmm_to_minute(draft.start_time or "00:00"), tz)
    if draft.end_time is None:
        end = local_to_utc(draft.end_date + timedelta(days=1), 0, tz)
    else:
        end = local_to_utc(draft.end_date, hhmm_to_minute(draft.end_time), tz)
    if not start < end:
        raise validation_failed(
            [{"path": "end_date", "message": "the exception must end after it starts"}]
        )
    payload = {
        "exception_id": str(uuid.uuid4()),
        "span": {"start": ts(start), "end": ts(end)},
        "blocks_all": draft.blocks_all,
        "blocks_tags": list(draft.blocks_tags),
        "reason": draft.reason,
    }
    return accepted(await stage(tx, user_id, [manual("exception_added", payload)], now=now))


@router.get("/exceptions/{exception_id}", operation_id="getException")
async def get_exception(
    user_id: UserId, tx: Tx, now: Now, exception_id: uuid.UUID
) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    row = await _get(tx, user_id, exception_id)
    if row is None:
        raise not_found("exception", exception_id)
    return exception_out(row)


@router.delete("/exceptions/{exception_id}", operation_id="deleteException", status_code=202)
async def delete_exception(
    user_id: UserId, tx: Tx, now: Now, exception_id: uuid.UUID
) -> JSONResponse:
    if await _get(tx, user_id, exception_id) is None:
        raise not_found("exception", exception_id)
    mutation = manual("exception_removed", {"exception_id": str(exception_id)})
    return accepted(await stage(tx, user_id, [mutation], now=now))

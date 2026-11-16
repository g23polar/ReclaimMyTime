"""PATCH /blocks/{id} (`blocks` tag). Blocks are never edited in place:

- complete / skip → `mark_block_state` (D5): applied directly, started blocks only → 200 Block;
- edit   → stage `pin_added` (kind=pin, the block's project, the new absolute span; D7) → 202;
- delete → stage `pin_added` (kind=forbid, the block's project, its span; D6) → 202.
If the block currently realizes a pin, `pin_removed` for it is staged first (M1+M2 gate).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.busy import Busy
from app.api.deps import (
    Maker,
    Now,
    RawBody,
    UserId,
    not_found,
    parse_body,
    transaction,
    validation_failed,
)
from app.api.mappers import block_out, ts
from app.api.schemas import BLOCK_ACTION_TAGS, BlockAction, BlockComplete, BlockEdit, BlockSkip
from app.api.staging import manual, stage_request
from app.auth.dependencies import require_timezone
from app.db.models import Block, BlockPin, WeekPlan
from app.db.models.staged import StagedMutation
from app.db.write_paths import lock_user, mark_block_state
from app.errors import AppError

router = APIRouter(tags=["blocks"], dependencies=[Depends(require_timezone)])

_ACTION: TypeAdapter[Any] = TypeAdapter(BlockAction)


@router.patch("/blocks/{block_id}", operation_id="patchBlock")
async def patch_block(
    user_id: UserId, maker: Maker, now: Now, busy: Busy, block_id: uuid.UUID, body: RawBody
) -> JSONResponse:
    action = parse_body(_ACTION, body, union_tags=BLOCK_ACTION_TAGS)

    if isinstance(action, BlockComplete | BlockSkip):
        async with transaction(maker) as tx:
            await lock_user(tx, user_id)
            state = "completed" if isinstance(action, BlockComplete) else "skipped"
            await mark_block_state(tx, user_id, block_id, state, now=now)
            block = (
                await tx.execute(
                    select(Block)
                    .where(Block.id == block_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            return JSONResponse(status_code=200, content=block_out(block))

    async def build(tx: AsyncSession) -> list[StagedMutation]:
        # Locked as before T4.2; the validation pass and the authoritative pass each run in
        # their own transaction, with the busy read between them (stage_request).
        await lock_user(tx, user_id)
        return await _pin_mutations(tx, user_id, block_id, action, now)

    return await stage_request(maker, user_id, build, now=now, busy=busy)


async def _pin_mutations(
    tx: AsyncSession, user_id: uuid.UUID, block_id: uuid.UUID, action: Any, now: datetime
) -> list[StagedMutation]:
    """edit → pin, delete → forbid (D6/D7), after the live/immutable/span checks."""
    row = (
        await tx.execute(
            select(Block, WeekPlan)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(Block.id == block_id, Block.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise not_found("block", block_id)
    block, plan = row
    if plan.status != "live":
        raise AppError(409, "block_superseded", "This block is not in the current live plan.")
    assert block.span.lower is not None and block.span.upper is not None
    if block.span.lower <= now:
        raise AppError(
            409, "block_immutable", "Past and in-progress blocks cannot be edited or deleted."
        )

    if isinstance(action, BlockEdit):
        _check_edit_span(action, plan, now)
        kind, start, end = "pin", action.span.start, action.span.end
    else:
        kind, start, end = "forbid", block.span.lower, block.span.upper
    payload: dict[str, Any] = {
        "pin_id": str(uuid.uuid4()),
        "project_id": str(block.project_id),
        "span": {"start": ts(start), "end": ts(end)},
        "kind": kind,
        "created_from_block_id": str(block.id),
    }
    # M1+M2 gate: a block that realizes a pin (exact project + span, the iOS G2 join) is
    # re-placed by replacing that pin, not by stacking a second one — both changes are
    # staged in this one changeset.
    realized = (
        await tx.execute(
            select(BlockPin.id)
            .where(
                BlockPin.user_id == user_id,
                BlockPin.kind == "pin",
                BlockPin.project_id == block.project_id,
                BlockPin.span == block.span,
            )
            .order_by(BlockPin.id)
        )
    ).scalars()
    mutations = [manual("pin_removed", {"pin_id": str(pin_id)}) for pin_id in realized]
    mutations.append(manual("pin_added", payload))
    return mutations


def _check_edit_span(action: BlockEdit, plan: WeekPlan, now: Any) -> None:
    """D7: the new span starts after now and lies inside the block's plan horizon."""
    h = plan.horizon
    assert h.lower is not None and h.upper is not None
    problems = []
    if not action.span.start > now:
        problems.append({"path": "span.start", "message": "must be after the current time"})
    if action.span.start < h.lower or action.span.end > h.upper:
        problems.append({"path": "span", "message": "must lie within the plan's week"})
    if problems:
        raise validation_failed(problems, "The edited block must start in the future, this week.")

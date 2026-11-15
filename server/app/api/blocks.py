"""PATCH /blocks/{id} (`blocks` tag). Blocks are never edited in place:

- complete / skip → `mark_block_state` (D5): applied directly, started blocks only → 200 Block;
- edit   → stage `pin_added` (kind=pin, the block's project, the new absolute span; D7) → 202;
- delete → stage `pin_added` (kind=forbid, the block's project, its span; D6) → 202.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import and_, select

from app.api.deps import Now, RawBody, Tx, UserId, not_found, parse_body, validation_failed
from app.api.mappers import block_out, ts
from app.api.schemas import BLOCK_ACTION_TAGS, BlockAction, BlockComplete, BlockEdit, BlockSkip
from app.api.staging import accepted, manual, stage
from app.auth.dependencies import require_timezone
from app.db.models import Block, WeekPlan
from app.db.write_paths import lock_user, mark_block_state
from app.errors import AppError

router = APIRouter(tags=["blocks"], dependencies=[Depends(require_timezone)])

_ACTION: TypeAdapter[Any] = TypeAdapter(BlockAction)


@router.patch("/blocks/{block_id}", operation_id="patchBlock")
async def patch_block(
    user_id: UserId, tx: Tx, now: Now, block_id: uuid.UUID, body: RawBody
) -> JSONResponse:
    action = parse_body(_ACTION, body, union_tags=BLOCK_ACTION_TAGS)
    await lock_user(tx, user_id)

    if isinstance(action, BlockComplete | BlockSkip):
        state = "completed" if isinstance(action, BlockComplete) else "skipped"
        await mark_block_state(tx, user_id, block_id, state, now=now)
        block = (
            await tx.execute(
                select(Block).where(Block.id == block_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        return JSONResponse(status_code=200, content=block_out(block))

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
    return accepted(await stage(tx, user_id, [manual("pin_added", payload)], now=now))


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

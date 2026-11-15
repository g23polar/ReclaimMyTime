"""Plans (`plans` tag): the live plan covering now, and per-lineage diffs between plans."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import Now, Tx, UserId, not_found
from app.api.views import diff_view, get_plan, plan_view
from app.auth.dependencies import require_timezone
from app.db.models import WeekPlan
from app.db.write_paths import sweep_past_blocks
from app.errors import AppError

router = APIRouter(tags=["plans"], dependencies=[Depends(require_timezone)])


@router.get("/plans/current", operation_id="getCurrentPlan")
async def get_current_plan(user_id: UserId, tx: Tx, now: Now) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)  # past `planned` blocks come back `completed`
    plan = (
        await tx.execute(
            select(WeekPlan)
            .where(
                WeekPlan.user_id == user_id,
                WeekPlan.status == "live",
                func.lower(WeekPlan.horizon) <= now,
                func.upper(WeekPlan.horizon) > now,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is None:  # D22
        raise AppError(404, "no_live_plan", "No live plan covers the current time.")
    return await plan_view(tx, user_id, plan)


@router.get("/plans/{plan_id}/diff", operation_id="getPlanDiff")
async def get_plan_diff(
    user_id: UserId, tx: Tx, now: Now, plan_id: uuid.UUID, against: Annotated[uuid.UUID, Query()]
) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    after = await get_plan(tx, user_id, plan_id)
    if after is None:
        raise not_found("plan", plan_id)
    before = await get_plan(tx, user_id, against)
    if before is None:
        raise not_found("plan", against)
    return await diff_view(tx, after, before)

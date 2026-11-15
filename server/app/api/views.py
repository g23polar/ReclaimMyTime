"""Read-side assembly of response bodies that span several tables (plans, changesets, diffs).

Reads only, through the caller's transaction; the mapping itself is in `mappers` (pure).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mappers import DiffBlock, changeset_out, diff_out, plan_out
from app.db.models import Block, Changeset, WeekPlan
from app.db.solver_io import completed_minutes


async def plan_blocks(tx: AsyncSession, plan_id: uuid.UUID) -> list[Block]:
    return list(
        (
            await tx.execute(
                select(Block)
                .where(Block.plan_id == plan_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def get_plan(tx: AsyncSession, user_id: uuid.UUID, plan_id: uuid.UUID) -> WeekPlan | None:
    return (
        await tx.execute(
            select(WeekPlan)
            .where(WeekPlan.id == plan_id, WeekPlan.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _needs_completed_minutes(plan: WeekPlan) -> bool:
    relaxations = (plan.infeasibility or {}).get("relaxations", [])
    return any(r.get("kind") == "reduce_estimate" for r in relaxations)


async def plan_view(tx: AsyncSession, user_id: uuid.UUID, plan: WeekPlan) -> dict[str, Any]:
    """Reads only this plan's blocks. The history-wide completed-minutes aggregate runs only
    when a `reduce_estimate` relaxation needs it (estimate = completed + proposed remaining)."""
    completed = await completed_minutes(tx, user_id) if _needs_completed_minutes(plan) else {}
    return plan_out(plan, await plan_blocks(tx, plan.id), completed)


async def diff_view(tx: AsyncSession, after: WeekPlan, before: WeekPlan | None) -> dict[str, Any]:
    after_blocks = [DiffBlock.of(b) for b in await plan_blocks(tx, after.id)]
    before_blocks = (
        [] if before is None else [DiffBlock.of(b) for b in await plan_blocks(tx, before.id)]
    )
    return diff_out(
        str(after.id), None if before is None else str(before.id), after_blocks, before_blocks
    )


async def _against(
    tx: AsyncSession, user_id: uuid.UUID, cs: Changeset, plan: WeekPlan
) -> WeekPlan | None:
    """D18: the live plan at proposal time. For a confirmed changeset that is the plan its
    confirm superseded (superseded_at == resolved_at, both stamped with the same `now`);
    otherwise the current live plan over the horizon (only this changeset's confirm could
    have replaced it, D4).

    TODO(contract): a week-start change can supersede two live plans; the diff has one
    `against_plan_id`, so the most recent is used."""
    query = select(WeekPlan).where(
        WeekPlan.user_id == user_id,
        WeekPlan.id != plan.id,
        WeekPlan.horizon.overlaps(plan.horizon),
    )
    if cs.status == "confirmed":
        query = query.where(
            WeekPlan.status == "superseded", WeekPlan.superseded_at == cs.resolved_at
        )
    else:
        query = query.where(WeekPlan.status == "live")
    query = query.order_by(WeekPlan.created_at.desc(), WeekPlan.version.desc()).limit(1)
    return (await tx.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


async def changeset_view(
    tx: AsyncSession, user_id: uuid.UUID, changeset_id: uuid.UUID
) -> dict[str, Any]:
    cs = (
        await tx.execute(
            select(Changeset)
            .where(Changeset.id == changeset_id, Changeset.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if cs.plan_id is None:
        return changeset_out(cs, None, None)
    plan = await get_plan(tx, user_id, cs.plan_id)
    assert plan is not None, "changesets.plan_id is ON DELETE SET NULL"
    against = await _against(tx, user_id, cs, plan)
    return changeset_out(cs, await plan_view(tx, user_id, plan), await diff_view(tx, plan, against))

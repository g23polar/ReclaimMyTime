"""M1+M2 gate (reviews/m1-m2-gate-2026-11-15.md): ended-but-unswept work is credited before
any solve (finding 2), and confirm refuses a proposal whose week has ended (finding 3)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Block, WeekPlan
from app.db.solver_io import to_interval
from app.db.write_paths import ChangesetStale, expected_horizon
from app.scheduler import SolverInput
from tests.db.conftest import (
    PORTFOLIO,
    T0,
    TZ,
    at,
    bootstrap,
    confirm,
    exception_added,
    prefs,
    propose,
    requires_db,
    rows,
)

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]


async def _blocks(maker: Maker, plan_id: uuid.UUID) -> list[Block]:
    async with maker() as s:
        return list((await s.execute(select(Block).where(Block.plan_id == plan_id))).scalars())


def _minutes(blocks: list[Block], project: uuid.UUID) -> int:
    return sum(
        int((b.span.upper - b.span.lower).total_seconds() // 60)
        for b in blocks
        if b.project_id == project
    )


# ---------------------------------------------------------------- finding 2


async def test_week_start_change_credits_unswept_ended_work(
    maker: Maker, user_id: uuid.UUID
) -> None:
    v1 = await bootstrap(maker, user_id)  # Monday-start week, confirmed Mon 06:00
    tuesday = at(1, "06:00")
    monday = [b for b in await _blocks(maker, v1) if b.span.upper <= tuesday]
    done = _minutes(monday, PORTFOLIO)
    assert done > 0, "the scenario needs Portfolio work on Monday"
    # No read in between: Monday's blocks are still `planned` when the change is proposed.
    assert {b.state for b in monday} == {"planned"}

    cs = await propose(maker, user_id, [prefs(week_start=2)], tuesday, horizon=None)
    plan_id = (await rows(maker, "SELECT plan_id FROM changesets WHERE id = :c", c=cs))[0][0]
    async with maker() as s:
        plan = (await s.execute(select(WeekPlan).where(WeekPlan.id == plan_id))).scalar_one()
    solved = SolverInput.model_validate(plan.solver_input)
    assert to_interval(plan.horizon) == expected_horizon(tuesday, TZ, 2)
    portfolio = next(p for p in solved.projects if p.id == str(PORTFOLIO))
    assert portfolio.remaining_minutes == 360 - done
    assert _minutes(await _blocks(maker, plan_id), PORTFOLIO) <= 360 - done

    result = await confirm(maker, user_id, cs, tuesday)
    live = await _blocks(maker, result.plan_id)
    assert _minutes(live, PORTFOLIO) <= 360 - done


# ---------------------------------------------------------------- finding 3


async def test_confirm_refuses_an_empty_proposal_after_rollover(
    maker: Maker, user_id: uuid.UUID
) -> None:
    cs = await propose(maker, user_id, [prefs()])  # no projects: an empty schedule
    next_week = T0 + timedelta(days=7)
    with pytest.raises(ChangesetStale) as err:
        await confirm(maker, user_id, cs, next_week)
    assert err.value.fresh_input.horizon == expected_horizon(next_week, TZ, 1)
    assert await rows(maker, "SELECT count(*) FROM user_preferences") == [(0,)]
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=cs) == [
        ("pending",)
    ]


async def test_confirm_refuses_a_fully_frozen_proposal_after_rollover(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    sunday_night = at(6, "23:00")  # every block of the week has ended
    cs = await propose(
        maker, user_id, [exception_added(at(6, "23:00"), at(7, "00:00"))], sunday_night
    )
    next_week = at(7, "06:00")
    with pytest.raises(ChangesetStale) as err:
        await confirm(maker, user_id, cs, next_week)
    assert err.value.fresh_input.horizon == expected_horizon(next_week, TZ, 1)
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]

"""End-to-end onboarding against Postgres (contracts NOTES "Story"): a project staged before
preferences exist has no plan; the preferences write then produces the first live plan.

(File name is historical: it began as a scratch smoke test and was repurposed rather than
deleted, per the no-deletion rule in force for T2.1.)"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.db.conftest import (
    WORKOUT,
    bootstrap,
    confirm,
    prefs,
    propose,
    requires_db,
    rows,
    workout,
)

pytestmark = requires_db


async def test_onboarding_planless_changeset_then_first_plan(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    c1 = await propose(maker, user_id, [workout()])
    assert await rows(maker, "SELECT plan_id FROM changesets WHERE id = :c", c=c1) == [(None,)]
    result = await confirm(maker, user_id, c1)
    assert result.plan_id is None and not result.already_confirmed
    assert await rows(maker, "SELECT status::text FROM projects WHERE id = :p", p=WORKOUT) == [
        ("active",)
    ]
    assert await rows(maker, "SELECT tag FROM user_tags") == [("gym",)]

    c2 = await propose(maker, user_id, [prefs()])
    live = (await confirm(maker, user_id, c2)).plan_id
    got = await rows(
        maker,
        "SELECT status::text, version, infeasibility IS NULL, solver_input->>'solver_version'"
        " FROM week_plans WHERE id = :p",
        p=live,
    )
    assert got == [("live", 1, True, "1.0.0")]
    minutes = await rows(
        maker,
        "SELECT sum(extract(epoch FROM upper(span) - lower(span)) / 60)::int, "
        " bool_and(gcal_status = 'pending') FROM blocks WHERE plan_id = :p",
        p=live,
    )
    assert minutes == [(180, True)]  # 3.0 weekly hours, all queued for write-back


async def test_bootstrap_helper_yields_a_clean_live_plan(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    plan = await bootstrap(maker, user_id)
    assert await rows(maker, "SELECT infeasibility FROM week_plans WHERE id = :p", p=plan) == [
        (None,)
    ]

"""Regression tests for the T2.1 code review (2026-11-15): partial-horizon promotion,
planless staging over an existing schedule, and the pending-changeset index."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.solver_io import build_solver_input, completed_minutes, week_horizon
from app.db.write_paths import (
    InvalidMutation,
    apply_mutation,
    persist_plan,
    stage_changeset,
)
from app.scheduler import pack
from tests.db.conftest import (
    HORIZON,
    PORTFOLIO,
    T0,
    TZ,
    WORKOUT,
    at,
    bootstrap,
    confirm,
    deadline_project,
    exception_added,
    prefs,
    propose,
    requires_db,
    rows,
    staged,
    workout,
)

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
TUE_WEEK = week_horizon(date(2026, 11, 17), TZ)  # week_start = 2 (Tuesday)


# ---------------------------------------------------------------- [major] partial horizon


async def test_week_start_change_keeps_completed_effort_of_superseded_plan(
    maker: Maker, user_id: uuid.UUID
) -> None:
    """Review scenario: a Mon–Mon live plan is replaced by a Tue–Tue plan after the week
    start moves. Monday's completed blocks lie outside the new horizon; their effort must
    still be credited (no sweep beforehand: confirm must sweep before superseding)."""
    v1 = await bootstrap(maker, user_id)
    now = at(1, "10:30")  # Tue 10:30 local: Monday + Tuesday-morning blocks are past
    past = await rows(
        maker,
        "SELECT project_id, sum(extract(epoch FROM upper(span) - lower(span)) / 60)::int"
        " FROM blocks WHERE plan_id = :p AND upper(span) <= :now GROUP BY project_id",
        p=v1,
        now=now,
    )
    expected = {str(pid): m for pid, m in past}
    assert set(expected) == {str(WORKOUT), str(PORTFOLIO)}

    cs = await propose(maker, user_id, [prefs(week_start=2)], now, TUE_WEEK)
    v2 = (await confirm(maker, user_id, cs, now)).plan_id
    assert await rows(maker, "SELECT status::text FROM week_plans WHERE id = :p", p=v1) == [
        ("superseded",)
    ]
    async with maker() as s:
        assert await completed_minutes(s, user_id) == expected
        inp = await build_solver_input(s, user_id, TUE_WEEK, now)
    portfolio = next(p for p in inp.projects if p.id == str(PORTFOLIO))
    assert portfolio.remaining_minutes == 6 * 60 - expected[str(PORTFOLIO)]
    assert v2 is not None


async def test_horizon_must_be_the_current_preference_week(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    with pytest.raises(InvalidMutation) as err:  # Tue–Tue horizon while week_start is Monday
        await propose(
            maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))], T0, TUE_WEEK
        )
    assert err.value.code == "validation_failed"
    with pytest.raises(InvalidMutation):  # persist_plan enforces it too
        async with maker() as s, s.begin():
            inp = await build_solver_input(s, user_id, TUE_WEEK, T0)
            await persist_plan(s, user_id, inp, pack(inp), now=T0, timezone=TZ)
    with pytest.raises(InvalidMutation):  # last week's horizon once `now` is in this week
        await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))],
                      at(7, "09:00"), HORIZON)  # fmt: skip


async def test_refuses_to_drop_future_blocks_outside_the_new_horizon(
    maker: Maker, user_id: uuid.UUID
) -> None:
    """Week start moves BACK (Tue → Mon): the old Tue–Tue plan's Monday-23 blocks are in the
    future and outside the new Mon 16–Mon 23 horizon. Superseding would silently drop them.
    A deadline project (9 h due Nov 30: 270 min this week, three 90-min sittings), because a
    perennial now gets at most one block a day and Monday is the only open day."""
    monday_only = {"mon": [["08:00", "20:00"]]}
    onboard = at(1, "06:00")
    cs = await propose(
        maker, user_id,
        [prefs(week_start=2, schedulable_windows=monday_only), deadline_project(hours=9.0)],
        onboard, TUE_WEEK,
    )  # fmt: skip
    old = (await confirm(maker, user_id, cs, onboard)).plan_id
    assert await rows(
        maker, "SELECT count(*) FROM blocks WHERE plan_id = :p AND lower(span) >= :e",
        p=old, e=HORIZON.end,
    ) == [(3,)]  # fmt: skip

    later = at(2, "10:00")  # Wednesday
    with pytest.raises(InvalidMutation) as err:
        await propose(
            maker, user_id, [prefs(week_start=1, schedulable_windows=monday_only)], later, HORIZON
        )
    assert err.value.code == "validation_failed"
    assert await rows(maker, "SELECT status::text FROM week_plans WHERE id = :p", p=old) == [
        ("live",)
    ]


# ---------------------------------------------------------------- [major] planless staging


async def test_planless_staging_refused_once_preferences_exist(
    maker: Maker, user_id: uuid.UUID
) -> None:
    """Review scenario: a blackout staged without a solve over a live plan."""
    await bootstrap(maker, user_id)
    with pytest.raises(InvalidMutation) as err:
        async with maker() as s, s.begin():
            await stage_changeset(
                s, user_id, staged(exception_added(at(2, "00:00"), at(3, "00:00"))), now=T0
            )
    assert err.value.code == "validation_failed"


async def test_planless_staging_refused_when_the_list_sets_preferences(
    maker: Maker, user_id: uuid.UUID
) -> None:
    with pytest.raises(InvalidMutation):
        async with maker() as s, s.begin():
            await stage_changeset(s, user_id, staged(prefs(), workout()), now=T0)


async def test_confirm_rechecks_planless_changeset(maker: Maker, user_id: uuid.UUID) -> None:
    cs = await propose(maker, user_id, [workout()])  # legitimately planless: no preferences
    async with maker() as s, s.begin():  # preferences appear by another path
        await apply_mutation(s, user_id, staged(prefs())[0], now=T0)
    with pytest.raises(InvalidMutation) as err:
        await confirm(maker, user_id, cs)
    assert err.value.code == "validation_failed"
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=cs) == [
        ("pending",)
    ]


# ---------------------------------------------------------------- [minor] pending index


async def test_pending_changeset_partial_index_exists(maker: Maker) -> None:
    got = await rows(
        maker,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
        " AND tablename = 'changesets' AND indexname = 'changesets_user_id_pending_idx'",
    )
    assert len(got) == 1
    assert "(user_id)" in got[0][0] and "WHERE (status = 'pending'" in got[0][0]

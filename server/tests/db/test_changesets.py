"""Changeset lifecycle against Postgres: concurrency, O8 stale, O9 carry-forward, outbox
inheritance, typed errors, and the DST projection through build_solver_input."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User
from app.db.solver_io import build_solver_input, week_horizon
from app.db.write_paths import (
    BlockNotFound,
    BlockNotStarted,
    BlockSuperseded,
    ChangesetNotFound,
    ChangesetNotPending,
    ChangesetStale,
    InvalidMutation,
    apply_mutation,
    carry_forward,
    lock_user,
    mark_block_state,
    stage_changeset,
)
from app.scheduler import Interval
from tests.db.conftest import (
    EXC,
    GRANT,
    HORIZON,
    T0,
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


async def _waiters(maker: Maker, n: int) -> None:
    """Block until `n` sessions are queued on an advisory lock (proves real contention)."""
    for _ in range(500):
        got = await rows(
            maker, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        )
        if got[0][0] >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} advisory-lock waiters")


async def _contend(maker: Maker, user: uuid.UUID, *coros: Any) -> list[Any]:
    """Start `coros` while a third session holds the user's lock, wait until all of them are
    blocked on it, then release: they run strictly one after another."""
    async with maker() as holder:
        await holder.begin()
        await lock_user(holder, user)
        tasks = [asyncio.create_task(c) for c in coros]
        await _waiters(maker, len(coros))
        await holder.rollback()
    return await asyncio.gather(*tasks, return_exceptions=True)


async def _live_count(maker: Maker) -> int:
    return (await rows(maker, "SELECT count(*) FROM week_plans WHERE status = 'live'"))[0][0]


# ---------------------------------------------------------------- concurrency


async def test_concurrent_confirm_of_one_changeset_applies_once(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])
    results = await _contend(
        maker, user_id, confirm(maker, user_id, cs), confirm(maker, user_id, cs)
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert sorted(r.already_confirmed for r in results) == [False, True]
    assert await _live_count(maker) == 1
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(1,)]
    assert await rows(
        maker, "SELECT count(*) FROM mutations WHERE kind = 'plan_confirmed' AND changeset_id = :c",
        c=cs,
    ) == [(1,)]  # fmt: skip


async def test_concurrent_confirm_of_two_changesets_same_week(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    a = uuid.uuid4()
    c1 = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"), a)])
    c2 = await propose(maker, user_id, [exception_added(at(4, "00:00"), at(5, "00:00"))])
    r1, r2 = await _contend(
        maker, user_id, confirm(maker, user_id, c1), confirm(maker, user_id, c2)
    )
    assert isinstance(r1, ChangesetNotPending) and r1.status == "expired"
    assert not isinstance(r2, BaseException) and not r2.already_confirmed
    assert await _live_count(maker) == 1
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions WHERE id = :a", a=a) == [
        (0,)
    ]


async def test_confirm_races_a_new_proposal(maker: Maker, user_id: uuid.UUID) -> None:
    """Task 1 confirms c1 while task 2 proposes-and-confirms c2. Either order is valid; in
    both, exactly one plan is live and the loser gets a typed refusal or builds on top."""
    await bootstrap(maker, user_id)
    c1 = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])

    async def second() -> Any:
        ex = exception_added(at(4, "00:00"), at(5, "00:00"), uuid.uuid4())
        c2 = await propose(maker, user_id, [ex])
        return await confirm(maker, user_id, c2)

    r1, r2 = await _contend(maker, user_id, confirm(maker, user_id, c1), second())
    assert not isinstance(r2, BaseException), r2
    assert r1.__class__.__name__ in ("ConfirmResult", "ChangesetNotPending", "ChangesetStale")
    assert await _live_count(maker) == 1


# ---------------------------------------------------------------- O8 stale proposals


async def test_o8_first_proposal_goes_stale_when_its_block_would_have_started(
    maker: Maker, user_id: uuid.UUID
) -> None:
    cs = await propose(maker, user_id, [prefs(), workout()])
    later = at(0, "08:30")  # the proposal's Monday 08:00 block is now in the past
    with pytest.raises(ChangesetStale) as err:
        await confirm(maker, user_id, cs, later)
    assert "differs" in err.value.reason
    assert all(b.span.start >= later for b in err.value.fresh_output.blocks)
    # Nothing applied.
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=cs) == [
        ("pending",)
    ]
    assert await rows(maker, "SELECT count(*) FROM user_preferences") == [(0,)]
    assert await _live_count(maker) == 0

    # T2.3's recovery: stage the fresh re-solve as a replacement, then confirm it.
    async with maker() as s, s.begin():
        old = await s.execute(text("SELECT staged FROM changesets WHERE id = :c"), {"c": cs})
        fresh = await stage_changeset(
            s,
            user_id,
            old.scalar_one(),
            err.value.fresh_input,
            err.value.fresh_output,
            now=later,
        )
    assert (await confirm(maker, user_id, fresh, later)).plan_id is not None
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=cs) == [
        ("expired",)
    ]


async def test_o8_stale_when_a_live_block_starts_before_confirm(
    maker: Maker, user_id: uuid.UUID
) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])
    with pytest.raises(ChangesetStale) as err:
        await confirm(maker, user_id, cs, at(0, "08:30"))
    assert "started" in err.value.reason
    assert [f.span.start for f in err.value.fresh_input.frozen] == [at(0, "08:00")]
    assert await rows(maker, "SELECT status::text FROM week_plans WHERE id = :p", p=v1) == [
        ("live",)
    ]


async def test_o8_not_stale_when_nothing_started(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])
    result = await confirm(maker, user_id, cs, T0 + timedelta(minutes=45))  # 06:45, pre-window
    assert result.plan_id is not None


# ---------------------------------------------------------------- O9 carry-forward


def _grant_update(hours: float) -> dict[str, Any]:
    return {
        "kind": "project_updated",
        "payload": {"project_id": str(GRANT), "changes": {"estimated_total_hours": hours}},
    }


async def test_o9_follow_up_on_staged_project_carries_it_forward(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    c1 = await propose(maker, user_id, [deadline_project(GRANT, 30.0, "2026-11-18", "Grant")])
    async with maker() as s, s.begin():
        carried = await carry_forward(s, user_id, staged(_grant_update(4.0)))
    assert [m.kind for m in carried] == ["project_created", "project_updated"]

    c2 = await propose(maker, user_id, [m.model_dump(mode="json") for m in carried])
    assert await rows(maker, "SELECT status::text FROM projects WHERE id = :p", p=GRANT) == [
        ("staged",)
    ]
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=c1) == [
        ("expired",)
    ]
    await confirm(maker, user_id, c2)
    assert await rows(
        maker, "SELECT status::text, estimated_total_hours FROM projects WHERE id = :p", p=GRANT
    ) == [("active", 4.0)]


async def test_o9_unrelated_follow_up_expires_the_staged_project(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    await propose(maker, user_id, [deadline_project(GRANT, 4.0, "2026-11-20", "Grant")])
    follow = [exception_added(at(4, "00:00"), at(5, "00:00"))]
    async with maker() as s, s.begin():
        carried = await carry_forward(s, user_id, staged(*follow))
    assert [m.kind for m in carried] == ["exception_added"]
    await propose(maker, user_id, follow)
    assert await rows(maker, "SELECT 1 FROM projects WHERE id = :p", p=GRANT) == []


# ---------------------------------------------------------------- outbox inheritance


async def test_confirm_inherits_gcal_identity_by_lineage(maker: Maker, user_id: uuid.UUID) -> None:
    v1 = await bootstrap(maker, user_id)
    async with maker() as s, s.begin():
        await s.execute(
            text(
                "UPDATE blocks SET gcal_status = 'synced', gcal_event_id = 'evt-' || lineage_id"
                " WHERE plan_id = :p"
            ),
            {"p": v1},
        )
    cs = await propose(maker, user_id, [exception_added(at(0, "08:00"), at(0, "10:00"))])
    v2 = (await confirm(maker, user_id, cs)).plan_id
    got = await rows(
        maker,
        "SELECT n.gcal_status::text, n.gcal_event_id, o.span = n.span, o.lineage_id IS NULL"
        " FROM blocks n LEFT JOIN blocks o ON o.plan_id = :v1 AND o.lineage_id = n.lineage_id"
        " WHERE n.plan_id = :v2",
        v1=v1,
        v2=v2,
    )
    for status, event, same_span, is_new in got:
        if is_new:
            assert (status, event) == ("pending", None)
        elif same_span:
            assert status == "synced" and event is not None
        else:
            assert status == "pending" and event is not None  # moved: PATCH the same event
    assert any(not same and not new for _, _, same, new in got), "expected a moved block"


# ---------------------------------------------------------------- typed errors


async def test_typed_errors(maker: Maker, user_id: uuid.UUID) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])

    other = uuid.uuid4()
    async with maker() as s, s.begin():
        s.add(User(id=other, created_at=T0))
    with pytest.raises(ChangesetNotFound):
        await confirm(maker, other, cs)

    first = await confirm(maker, user_id, cs)
    again = await confirm(maker, user_id, cs)
    assert again.already_confirmed and again.plan_id == first.plan_id

    future = (
        await rows(
            maker, "SELECT id FROM blocks WHERE plan_id = :p ORDER BY lower(span) DESC LIMIT 1",
            p=first.plan_id,
        )
    )[0][0]  # fmt: skip
    old = (await rows(maker, "SELECT id FROM blocks WHERE plan_id = :p LIMIT 1", p=v1))[0][0]
    for block, exc in (
        (future, BlockNotStarted),
        (old, BlockSuperseded),
        (uuid.uuid4(), BlockNotFound),
    ):
        with pytest.raises(exc):
            async with maker() as s, s.begin():
                await mark_block_state(s, user_id, block, "completed", now=T0)

    archive = staged({"kind": "project_archived", "payload": {"project_id": str(WORKOUT)}})[0]
    rename = {"project_id": str(WORKOUT), "changes": {"name": "Gym"}}
    update = staged({"kind": "project_updated", "payload": rename})[0]
    gone = staged({"kind": "exception_removed", "payload": {"exception_id": str(uuid.uuid4())}})[0]
    async with maker() as s, s.begin():
        await apply_mutation(s, user_id, archive, now=T0)
        with pytest.raises(InvalidMutation) as err:
            await apply_mutation(s, user_id, update, now=T0)
        assert err.value.code == "project_archived"
        with pytest.raises(InvalidMutation) as err:
            await apply_mutation(s, user_id, gone, now=T0)
        assert err.value.code == "not_found"
    # Re-using an existing id is refused at staging, not at confirm.
    with pytest.raises(InvalidMutation):
        await propose(maker, user_id, [workout()])
    with pytest.raises(InvalidMutation) as err:
        await propose(maker, user_id, [exception_added(at(5, "00:00"), at(6, "00:00"))])
    assert err.value.code == "exception_exists"


# ---------------------------------------------------------------- DST via the database


async def test_build_solver_input_dst_week_new_york(maker: Maker, user_id: uuid.UUID) -> None:
    tz = "America/New_York"
    now = datetime(2026, 10, 26, 4, tzinfo=UTC)
    deadline = deadline_project(GRANT, 2.0, "2026-11-01", "Grant")
    async with maker() as s, s.begin():
        for m in staged(prefs(timezone=tz), deadline):
            await apply_mutation(s, user_id, m, now=now)
    horizon = week_horizon(date(2026, 10, 26), tz)
    async with maker() as s:
        inp = await build_solver_input(s, user_id, horizon, now)
    assert inp.horizon.duration_minutes == 169 * 60
    by_start = {iv.start: iv for iv in inp.schedulable}
    sat, sun = datetime(2026, 10, 31, 12, tzinfo=UTC), datetime(2026, 11, 1, 13, tzinfo=UTC)
    assert by_start[sat] == Interval(start=sat, end=sat + timedelta(hours=12))  # 08-20 EDT
    assert by_start[sun] == Interval(start=sun, end=sun + timedelta(hours=12))  # 08-20 EST
    [project] = inp.projects
    assert project.deadline == datetime(2026, 11, 2, 5, tzinfo=UTC)  # end of Nov 1, EST
    assert project.remaining_minutes == 120
    assert HORIZON.duration_minutes == 168 * 60  # the LA test week has no transition


# ---------------------------------------------------------------- issue #7: tag cap

CAP_MESSAGE = "You can have at most 50 tags."


async def _seed_tags(maker: Maker, user: uuid.UUID, total: int) -> None:
    """Top `user_tags` up to `total` rows directly (staging would refuse first)."""
    have = (await rows(maker, "SELECT count(*) FROM user_tags WHERE user_id = :u", u=user))[0][0]
    async with maker() as s, s.begin():
        for i in range(total - have):
            await s.execute(
                text("INSERT INTO user_tags (user_id, tag) VALUES (:u, :t)"),
                {"u": user, "t": f"t{i:02}"},
            )


async def _tag_count(maker: Maker, user: uuid.UUID) -> int:
    return (await rows(maker, "SELECT count(*) FROM user_tags WHERE user_id = :u", u=user))[0][0]


def _tagged(tags: list[str], pid: uuid.UUID = GRANT) -> dict[str, Any]:
    m = workout(pid)
    m["payload"]["project"] |= {"name": "Tagged", "requirements": tags}
    return m


def _tag_exception(tags: list[str], eid: uuid.UUID = EXC) -> dict[str, Any]:
    m = exception_added(at(2, "00:00"), at(3, "00:00"), eid)
    m["payload"] |= {"blocks_all": False, "blocks_tags": tags}
    return m


async def test_tag_cap_allows_existing_tags_at_cap(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    await _seed_tags(maker, user_id, 50)
    cs = await propose(maker, user_id, [_tagged(["gym", "t00"]), _tag_exception(["t01"])])
    await confirm(maker, user_id, cs)
    assert await _tag_count(maker, user_id) == 50
    status = "SELECT status::text FROM projects WHERE id = :p"
    assert await rows(maker, status, p=GRANT) == [("active",)]


async def test_tag_cap_allows_existing_tags_over_cap(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    await _seed_tags(maker, user_id, 52)
    cs = await propose(maker, user_id, [_tagged(["t50"]), _tag_exception(["gym"])])
    await confirm(maker, user_id, cs)
    assert await _tag_count(maker, user_id) == 52


async def _refused_confirm(maker: Maker, user: uuid.UUID, cs: uuid.UUID, count: int) -> None:
    """The confirm fails as a whole: no tag, project, exception, plan or trace row stored."""
    with pytest.raises(InvalidMutation) as err:
        await confirm(maker, user, cs)
    assert err.value.code == "validation_failed"
    assert CAP_MESSAGE in err.value.message
    assert await _tag_count(maker, user) == count
    assert await rows(maker, "SELECT status::text FROM projects WHERE id = :p", p=GRANT) == [
        ("staged",)
    ]
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]
    assert await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=cs) == [
        ("pending",)
    ]
    assert await rows(maker, "SELECT count(*) FROM mutations WHERE changeset_id = :c", c=cs) == [
        (1,)  # plan_proposed only
    ]


async def test_tag_cap_refuses_a_new_tag_at_confirm(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [_tagged(["gym", "new"])])
    await _seed_tags(maker, user_id, 50)
    await _refused_confirm(maker, user_id, cs, 50)


async def test_tag_cap_refuses_a_changeset_crossing_it_part_way(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [_tagged(["a", "b"]), _tag_exception(["c"])])
    await _seed_tags(maker, user_id, 48)  # +2 fits, the exception's +1 does not
    await _refused_confirm(maker, user_id, cs, 48)


async def test_tag_cap_counts_a_repeated_new_tag_once(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    await _seed_tags(maker, user_id, 49)
    cs = await propose(maker, user_id, [_tagged(["new"]), _tag_exception(["new"])])
    await confirm(maker, user_id, cs)
    assert await _tag_count(maker, user_id) == 50


async def test_tag_cap_refuses_an_over_cap_list_at_staging(
    maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    await _seed_tags(maker, user_id, 49)
    pending = "SELECT count(*) FROM changesets WHERE status = 'pending'"
    for items in ([_tagged(["a", "b"])], [_tagged(["a"]), _tag_exception(["b"])]):
        with pytest.raises(InvalidMutation) as err:
            await propose(maker, user_id, items)
        assert err.value.code == "validation_failed"
        assert CAP_MESSAGE in err.value.message
        assert await rows(maker, pending) == [(0,)]
        assert await rows(maker, "SELECT count(*) FROM projects WHERE id = :p", p=GRANT) == [(0,)]
    update = {"project_id": str(WORKOUT), "changes": {"requirements": ["gym", "a", "b"]}}
    with pytest.raises(InvalidMutation, match=CAP_MESSAGE):
        async with maker() as s, s.begin():
            await stage_changeset(
                s, user_id, staged({"kind": "project_updated", "payload": update}), now=T0
            )
    assert await _tag_count(maker, user_id) == 49

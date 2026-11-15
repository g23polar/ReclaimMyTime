"""The 7 invariants from the spec sessions, as Postgres-backed pytest (TECH_SPEC T2.1)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.solver_io import build_solver_input
from app.db.write_paths import (
    ChangesetNotPending,
    PlanVerificationError,
    mark_block_state,
    persist_plan,
    sweep_past_blocks,
)
from app.scheduler import PlannedBlock, SolverOutput, pack
from tests.db.conftest import (
    EXC,
    GRANT,
    HORIZON,
    T0,
    TZ,
    WORKOUT,
    at,
    bootstrap,
    confirm,
    constraint_snapshot,
    deadline_project,
    exception_added,
    prefs,
    propose,
    reject,
    requires_db,
    rows,
)

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]


async def _count(maker: Maker, sql: str, **p: object) -> int:
    return (await rows(maker, sql, **p))[0][0]


def _mixed_change() -> list[dict[str, object]]:
    """A changeset touching every kind of constraint state."""
    forbid = {
        "kind": "pin_added",
        "payload": {
            "pin_id": str(uuid.uuid4()),
            "project_id": None,
            "kind": "forbid",
            "span": {"start": at(3, "08:00").isoformat(), "end": at(3, "10:00").isoformat()},
            "created_from_block_id": None,
        },
    }
    return [
        exception_added(at(2, "00:00"), at(3, "00:00")),
        {
            "kind": "project_updated",
            "payload": {"project_id": str(WORKOUT), "changes": {"weekly_target_hours": 2.0}},
        },
        deadline_project(GRANT, hours=4.0, target="2026-11-21", name="Grant"),
        forbid,
        prefs(buffer_minutes=30),
    ]


# (1) ----------------------------------------------------------------------------------


async def test_1_only_persist_plan_creates_blocks(maker: Maker, user_id: uuid.UUID) -> None:
    cs = await propose(maker, user_id, [prefs(), deadline_project()])
    before = await _count(maker, "SELECT count(*) FROM blocks")
    plans_before = await _count(maker, "SELECT count(*) FROM week_plans")
    assert before > 0
    await confirm(maker, user_id, cs)
    assert await _count(maker, "SELECT count(*) FROM blocks") == before
    assert await _count(maker, "SELECT count(*) FROM week_plans") == plans_before

    # persist_plan refuses a plan verify() rejects, writing nothing.
    async with maker() as s, s.begin():
        inp = await build_solver_input(s, user_id, HORIZON, T0)
        good = pack(inp)
        b = good.blocks[0]
        clash = PlannedBlock(lineage_id=str(uuid.uuid4()), project_id=b.project_id, span=b.span)
        bad = SolverOutput(solver_version=good.solver_version, blocks=(*good.blocks, clash))
        with pytest.raises(PlanVerificationError) as err:
            await persist_plan(s, user_id, inp, bad, now=T0, timezone=TZ)
        assert {"overlap", "duplicate_lineage"} & {v.kind for v in err.value.violations}
    assert await _count(maker, "SELECT count(*) FROM week_plans") == plans_before


# (2) ----------------------------------------------------------------------------------


async def test_2_one_live_plan_per_user_week(maker: Maker, user_id: uuid.UUID) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])
    v2 = (await confirm(maker, user_id, cs)).plan_id
    plans = await rows(
        maker, "SELECT id, status::text, superseded_at FROM week_plans ORDER BY version"
    )
    assert [(p[0], p[1]) for p in plans] == [(v1, "superseded"), (v2, "live")]
    assert plans[0][2] == T0
    with pytest.raises(IntegrityError, match="week_plans_one_live_excl"):
        async with maker() as s, s.begin():
            await s.execute(text("UPDATE week_plans SET status = 'live' WHERE id = :p"), {"p": v1})


# (3) ----------------------------------------------------------------------------------


async def test_3_one_proposed_plan_per_user_week(maker: Maker, user_id: uuid.UUID) -> None:
    await bootstrap(maker, user_id)
    c1 = await propose(maker, user_id, [exception_added(at(2, "00:00"), at(3, "00:00"))])
    c2 = await propose(maker, user_id, [exception_added(at(4, "00:00"), at(5, "00:00"))])
    got = await rows(
        maker,
        "SELECT c.id, c.status::text, p.status::text FROM changesets c"
        " JOIN week_plans p ON p.id = c.plan_id WHERE c.id IN (:a, :b) ORDER BY p.version",
        a=c1,
        b=c2,
    )
    assert got == [(c1, "expired", "rejected"), (c2, "pending", "proposed")]
    assert await _count(maker, "SELECT count(*) FROM week_plans WHERE status = 'proposed'") == 1

    with pytest.raises(IntegrityError, match="week_plans_one_proposed_excl"):
        async with maker() as s, s.begin():
            inp = await build_solver_input(s, user_id, HORIZON, T0)
            await persist_plan(s, user_id, inp, pack(inp), now=T0, timezone=TZ)


# (4) ----------------------------------------------------------------------------------


async def test_4_block_exclusion_rejects_overlap_within_a_plan(
    maker: Maker, user_id: uuid.UUID
) -> None:
    plan = await bootstrap(maker, user_id)
    insert = (
        "INSERT INTO blocks (plan_id, user_id, project_id, lineage_id, span)"
        " VALUES (:p, :u, :pr, gen_random_uuid(), tstzrange(:a, :b, '[)'))"
    )
    a, b, c = at(6, "18:00"), at(6, "19:00"), at(6, "20:00")
    async with maker() as s, s.begin():  # adjacent half-open spans do not overlap
        await s.execute(text(insert), {"p": plan, "u": user_id, "pr": WORKOUT, "a": a, "b": b})
        await s.execute(text(insert), {"p": plan, "u": user_id, "pr": WORKOUT, "a": b, "b": c})
    with pytest.raises(IntegrityError, match="blocks_no_overlap_excl"):
        async with maker() as s, s.begin():
            await s.execute(
                text(insert),
                {"p": plan, "u": user_id, "pr": WORKOUT, "a": a + timedelta(minutes=30), "b": c},
            )


# (5) ----------------------------------------------------------------------------------


async def test_5_past_blocks_immutable_across_resolve(maker: Maker, user_id: uuid.UUID) -> None:
    v1 = await bootstrap(maker, user_id)
    # The outbox worker (T4.3) has synced every v1 block.
    async with maker() as s, s.begin():
        await s.execute(
            text(
                "UPDATE blocks SET gcal_status = 'synced', gcal_event_id = 'evt-' || lineage_id,"
                " gcal_etag = 'etag-1' WHERE plan_id = :p"
            ),
            {"p": v1},
        )
    now = at(1, "10:30")  # Tue 10:30 local: Monday's and Tuesday-morning blocks are past
    async with maker() as s, s.begin():
        await sweep_past_blocks(s, user_id, at(0, "23:00"))  # Monday swept
        first = (
            await s.execute(
                text("SELECT id FROM blocks WHERE plan_id = :p ORDER BY lower(span) LIMIT 1"),
                {"p": v1},
            )
        ).scalar_one()
        await mark_block_state(s, user_id, first, "skipped", now=now)
    v1_rows = await rows(
        maker,
        "SELECT lineage_id, project_id, span, state::text, gcal_status::text, gcal_event_id,"
        " gcal_etag FROM blocks WHERE plan_id = :p AND lower(span) < :now ORDER BY lower(span)",
        p=v1,
        now=now,
    )
    assert len(v1_rows) >= 3 and {r[3] for r in v1_rows} >= {"skipped", "completed", "planned"}

    cs = await propose(maker, user_id, [exception_added(at(3, "00:00"), at(4, "00:00"))], now)
    async with maker() as s, s.begin():  # a sweep between proposal and confirm...
        await sweep_past_blocks(s, user_id, now)
    v2 = (await confirm(maker, user_id, cs, now)).plan_id
    assert v2 != v1

    v2_rows = await rows(
        maker,
        "SELECT lineage_id, project_id, span, state::text, gcal_status::text, gcal_event_id,"
        " gcal_etag FROM blocks WHERE plan_id = :p AND lower(span) < :now ORDER BY lower(span)",
        p=v2,
        now=now,
    )
    # ...is reflected: frozen copies carry the CURRENT state, verbatim otherwise.
    expected = [(*r[:3], "completed" if r[3] == "planned" else r[3], *r[4:]) for r in v1_rows]
    assert v2_rows == expected
    # Nothing new was placed in the past.
    assert (
        await _count(
            maker,
            "SELECT count(*) FROM blocks WHERE plan_id = :p AND lower(span) < :now"
            " AND lineage_id NOT IN (SELECT lineage_id FROM blocks WHERE plan_id = :old)",
            p=v2,
            now=now,
            old=v1,
        )
        == 0
    )


# (6) ----------------------------------------------------------------------------------


async def test_6_staged_writes_do_not_change_state_until_confirm(
    maker: Maker, user_id: uuid.UUID
) -> None:
    live = await bootstrap(maker, user_id)
    before = await constraint_snapshot(maker, user_id)
    cs = await propose(maker, user_id, _mixed_change())
    assert await constraint_snapshot(maker, user_id) == before
    assert await rows(maker, "SELECT status::text FROM week_plans WHERE id = :p", p=live) == [
        ("live",)
    ]
    # The staged project row exists (FK target for the proposal) but is not state.
    assert await rows(maker, "SELECT status::text FROM projects WHERE id = :p", p=GRANT) == [
        ("staged",)
    ]

    await confirm(maker, user_id, cs)
    after = await constraint_snapshot(maker, user_id)
    assert after != before
    assert [e[0] for e in after["exceptions"]] == [EXC]
    assert len(after["pins"]) == 1
    assert after["prefs"][0][2] == 30
    statuses = {p[0]: (p[2], p[4]) for p in after["projects"]}
    assert statuses[GRANT][0] == "active"
    assert float(statuses[WORKOUT][1]) == 2.0
    kinds = await rows(
        maker, "SELECT kind::text FROM mutations WHERE changeset_id = :c ORDER BY kind", c=cs
    )
    assert sorted(k[0] for k in kinds) == sorted(
        [
            "exception_added",
            "project_updated",
            "project_created",
            "pin_added",
            "preferences_updated",
            "plan_proposed",
            "plan_confirmed",
        ]
    )


# (7) ----------------------------------------------------------------------------------


async def test_7_reject_leaves_state_untouched_and_deletes_staged_projects(
    maker: Maker, user_id: uuid.UUID
) -> None:
    live = await bootstrap(maker, user_id)
    before = await constraint_snapshot(maker, user_id)
    cs = await propose(maker, user_id, _mixed_change())
    plan = (await rows(maker, "SELECT plan_id FROM changesets WHERE id = :c", c=cs))[0][0]

    await reject(maker, user_id, cs)
    assert await constraint_snapshot(maker, user_id) == before
    assert await rows(maker, "SELECT 1 FROM projects WHERE id = :p", p=GRANT) == []
    assert await rows(
        maker,
        "SELECT c.status::text, p.status::text, l.status::text FROM changesets c, week_plans p,"
        " week_plans l WHERE c.id = :c AND p.id = :p AND l.id = :l",
        c=cs,
        p=plan,
        l=live,
    ) == [("rejected", "rejected", "live")]

    await reject(maker, user_id, cs)  # idempotent
    with pytest.raises(ChangesetNotPending) as err:
        await confirm(maker, user_id, cs)
    assert err.value.status == "rejected"

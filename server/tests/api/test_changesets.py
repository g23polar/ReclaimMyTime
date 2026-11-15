"""Confirm / reject over HTTP: happy paths, idempotency, 409 changeset_not_pending, 404s and the
O8 changeset_stale recovery (fresh proposal staged in a new transaction)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import SettableClock, Who, new_user
from tests.api.contract import assert_conforms
from tests.db.conftest import T0, WORKOUT, at, bootstrap, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
WEDNESDAY = {"start_date": "2026-11-18", "end_date": "2026-11-18", "blocks_all": True}


async def _status(maker: Maker, cs: str) -> str:
    got = await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=uuid.UUID(cs))
    return got[0][0]


async def _stage_wednesday_off(client: httpx.AsyncClient) -> str:
    resp = await client.post("/exceptions", json=WEDNESDAY)
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


async def test_confirm_applies_and_promotes(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID
) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_conforms("confirmChangeset", 200, body)
    assert body["diff"]["against_plan_id"] == str(v1)
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(1,)]
    current = (await client.get("/plans/current")).json()
    assert current["id"] == body["plan_id"]
    old = await rows(maker, "SELECT status::text FROM week_plans WHERE id = :p", p=v1)
    assert old == [("superseded",)]


async def test_confirm_replay_is_idempotent_and_writes_nothing(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    first = await client.post(f"/changesets/{cs}/confirm")
    traces = await rows(maker, "SELECT count(*) FROM mutations")
    second = await client.post(f"/changesets/{cs}/confirm")
    assert (second.status_code, second.json()) == (200, first.json())
    assert await rows(maker, "SELECT count(*) FROM mutations") == traces


async def test_reject_leaves_state_untouched_and_is_idempotent(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID
) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    first = await client.post(f"/changesets/{cs}/reject")
    assert first.status_code == 200
    assert_conforms("rejectChangeset", 200, first.json())
    assert first.json()["status"] == "rejected" and first.json()["plan"]["status"] == "rejected"
    assert (await client.post(f"/changesets/{cs}/reject")).json() == first.json()
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]
    assert (await client.get("/plans/current")).json()["id"] == str(v1)


async def test_not_pending_conflicts_carry_the_status(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID
) -> None:
    await bootstrap(maker, user_id)
    expired = await _stage_wednesday_off(client)
    confirmed = await _stage_wednesday_off(client)  # D4: expires the first
    assert await _status(maker, expired) == "expired"
    assert (await client.post(f"/changesets/{confirmed}/confirm")).status_code == 200
    for cs, action, status in (
        (expired, "confirm", "expired"),
        (expired, "reject", "expired"),
        (confirmed, "reject", "confirmed"),
    ):
        resp = await client.post(f"/changesets/{cs}/{action}")
        assert resp.status_code == 409, (cs, action)
        assert resp.json()["error"]["code"] == "changeset_not_pending"
        assert resp.json()["error"]["details"] == {"status": status}
        assert_conforms("confirmChangeset", 409, resp.json())


async def test_unknown_or_foreign_changeset_is_404(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, who: Who
) -> None:
    await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    who.user_id = await new_user(maker)
    for path in (f"/changesets/{cs}/confirm", f"/changesets/{uuid.uuid4()}/reject"):
        resp = await client.post(path)
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["resource"] == "changeset"
    assert await _status(maker, cs) == "pending"


async def test_stale_confirm_stages_a_fresh_proposal(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, clock: SettableClock
) -> None:
    v1 = await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    clock.current = at(0, "08:30")  # Monday's 08:00 block started after the proposal
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "changeset_stale"
    assert_conforms("confirmChangeset", 409, resp.json())
    fresh = error["details"]["changeset_id"]
    assert fresh != cs
    # Nothing applied; the stale one expired; the fresh one is pending with the same staging.
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]
    assert (await _status(maker, cs), await _status(maker, fresh)) == ("expired", "pending")
    ok = await client.post(f"/changesets/{fresh}/confirm")
    assert ok.status_code == 200, ok.text
    assert ok.json()["diff"]["against_plan_id"] == str(v1)
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(1,)]


async def test_stale_after_week_rollover_resolves_over_the_new_week(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, clock: SettableClock
) -> None:
    await bootstrap(maker, user_id)
    resp = await client.patch(f"/projects/{WORKOUT}", json={"weekly_target_hours": 2.0})
    cs = resp.json()["id"]
    clock.current = T0 + timedelta(days=7)  # next Monday 06:00 local
    stale = await client.post(f"/changesets/{cs}/confirm")
    assert stale.status_code == 409, stale.text
    fresh = stale.json()["error"]["details"]["changeset_id"]
    ok = await client.post(f"/changesets/{fresh}/confirm")
    assert ok.status_code == 200, ok.text
    assert ok.json()["plan"]["week_start"] == "2026-11-23"


# ---------------------------------------------------------------- stale-recovery races


def _in_the_gap(monkeypatch, action) -> None:  # type: ignore[no-untyped-def]
    """Run `action` after the stale confirm rolled back and before the restage transaction —
    the window a concurrent request can commit in."""
    from app.api import changesets as module

    original = module._restage

    async def racing(*args, **kwargs):  # type: ignore[no-untyped-def]
        await action()
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "_restage", racing)


async def test_stale_recovery_does_not_revive_a_rejected_changeset(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, clock: SettableClock, monkeypatch
) -> None:
    await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    clock.current = at(0, "08:30")

    async def reject_it() -> None:
        from tests.db.conftest import reject

        await reject(maker, user_id, uuid.UUID(cs), clock.current)

    _in_the_gap(monkeypatch, reject_it)
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "changeset_not_pending"
    assert resp.json()["error"]["details"] == {"status": "rejected"}
    assert await _status(maker, cs) == "rejected"
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(0,)]


async def test_stale_recovery_does_not_replace_a_newer_proposal(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, clock: SettableClock, monkeypatch
) -> None:
    await bootstrap(maker, user_id)
    cs = await _stage_wednesday_off(client)
    clock.current = at(0, "08:30")
    newer: list[uuid.UUID] = []

    async def stage_newer() -> None:
        from tests.db.conftest import GRANT, deadline_project, propose

        grant = deadline_project(GRANT, hours=2.0, name="Grant")
        newer.append(await propose(maker, user_id, [grant], clock.current))

    _in_the_gap(monkeypatch, stage_newer)
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "changeset_not_pending"
    assert resp.json()["error"]["details"] == {"status": "expired"}
    # The newer proposal survives, with its staged project row.
    assert await _status(maker, str(newer[0])) == "pending"
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(1,)]
    assert await rows(maker, "SELECT status::text FROM projects WHERE name = 'Grant'") == [
        ("staged",)
    ]


# ---------------------------------------------------------------- week rollover at confirm


async def _assert_rolled_over(client: httpx.AsyncClient, maker: Maker, cs: str) -> None:
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "changeset_stale"
    fresh = resp.json()["error"]["details"]["changeset_id"]
    assert (await _status(maker, cs), await _status(maker, fresh)) == ("expired", "pending")
    plan = await rows(
        maker,
        "SELECT p.week_start FROM changesets c JOIN week_plans p ON p.id = c.plan_id"
        " WHERE c.id = :c",
        c=uuid.UUID(fresh),
    )
    assert str(plan[0][0]) == "2026-11-23"


async def test_rollover_of_an_empty_schedule_is_stale(
    client: httpx.AsyncClient, maker: Maker, clock: SettableClock
) -> None:
    prefs = {
        "timezone": "America/Los_Angeles",
        "schedulable_windows": {"mon": [["08:00", "20:00"]]},
        "day_diversity": "mixed",
        "buffer_minutes": 15,
        "week_start": 1,
    }
    resp = await client.put("/preferences", json=prefs)  # no projects: zero blocks
    assert resp.status_code == 202 and resp.json()["plan"]["blocks"] == []
    clock.current = T0 + timedelta(days=7)
    await _assert_rolled_over(client, maker, resp.json()["id"])
    assert await rows(maker, "SELECT count(*) FROM user_preferences") == [(0,)]


async def test_rollover_of_a_fully_frozen_schedule_is_stale(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, clock: SettableClock
) -> None:
    await bootstrap(maker, user_id)
    clock.current = at(6, "23:00")  # Sunday night: every block has ended
    sunday = {"start_date": "2026-11-22", "end_date": "2026-11-22", "blocks_all": True}
    resp = await client.post("/exceptions", json=sunday)
    assert resp.status_code == 202, resp.text
    clock.current = at(7, "06:00")
    await _assert_rolled_over(client, maker, resp.json()["id"])
    assert await rows(maker, "SELECT count(*) FROM availability_exceptions") == [(0,)]

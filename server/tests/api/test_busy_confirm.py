"""T4.2 confirm re-reads busy time (before the lock): a future proposed block overlapping fresh
busy → 409 `changeset_stale` with a fresh proposal solved against it; non-overlapping busy →
confirms; Google transient/revoked → 503/409 with nothing applied."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tests.api.conftest import NOW
from tests.api.contract import assert_conforms
from tests.api.test_busy_routes import BUSY, WEDNESDAY_OFF, ok, overlaps, solver_busy
from tests.db.conftest import WORKOUT, at, requires_db, rows
from tests.gcal.calendar_fakes import seed_connection, z
from tests.gcal.fakes import Reply

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]


async def exceptions(maker: Any) -> int:
    return (await rows(maker, "SELECT count(*) FROM availability_exceptions"))[0][0]


async def status(maker: Any, cs: str) -> str:
    got = await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=uuid.UUID(cs))
    return got[0][0]


def first_future(body: dict[str, Any]) -> tuple[datetime, datetime]:
    spans = sorted(
        (datetime.fromisoformat(b["span"]["start"]), datetime.fromisoformat(b["span"]["end"]))
        for b in body["plan"]["blocks"]
    )
    return next(s for s in spans if s[0] >= NOW)


async def staged(client: httpx.AsyncClient, maker: Any, uid: uuid.UUID, cipher: Any) -> Any:
    await seed_connection(maker, uid, cipher, now=NOW)
    return await ok(client.post("/exceptions", json=WEDNESDAY_OFF))


async def test_busy_window_displaces_blocks_the_unconnected_solve_used(
    gcal, cal, cipher, client, maker, who
):
    """Control for the route tests: without busy the solver does use Wed/Thu."""
    plan = await ok(client.get("/plans/current"), 200)
    assert any(overlaps(b["span"], *BUSY[0]) for b in plan["blocks"] if b["state"] == "planned")
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cal.busy = list(BUSY)
    reading = {"name": "Reading", "kind": "perennial", "weekly_target_hours": 2.0}
    body = await ok(client.post("/projects", json=reading | {"max_block_minutes": 60}))
    assert not any(overlaps(b["span"], *BUSY[0]) for b in body["plan"]["blocks"])


async def test_fresh_busy_over_a_future_block_is_stale_with_a_new_proposal(
    cal, cipher, client, maker, who
):
    body = await staged(client, maker, who.user_id, cipher)
    hit = first_future(body)
    cal.busy = [hit]  # a meeting landed on the proposed block after the proposal
    resp = await client.post(f"/changesets/{body['id']}/confirm")
    assert resp.status_code == 409, resp.text
    assert_conforms("confirmChangeset", 409, resp.json())
    error = resp.json()["error"]
    assert error["code"] == "changeset_stale"
    fresh = error["details"]["changeset_id"]
    assert await exceptions(maker) == 0  # nothing applied
    assert (await status(maker, body["id"]), await status(maker, fresh)) == ("expired", "pending")
    plan_id = (
        await rows(maker, "SELECT plan_id FROM changesets WHERE id = :c", c=uuid.UUID(fresh))
    )[0][0]
    assert await solver_busy(maker, str(plan_id)) == [{"start": z(hit[0]), "end": z(hit[1])}]
    blocks = await rows(
        maker, "SELECT lower(span), upper(span) FROM blocks WHERE plan_id = :p", p=plan_id
    )
    assert not any(s < hit[1] and hit[0] < e for s, e in blocks)
    assert len(cal.freebusy_bodies) == 2  # staging + confirm; the restage reuses confirm's read


async def test_fresh_busy_elsewhere_still_confirms(cal, cipher, client, maker, who):
    body = await staged(client, maker, who.user_id, cipher)
    cal.busy = [(at(5, "05:00"), at(5, "06:00"))]  # Saturday before any window: no block there
    confirmed = await ok(client.post(f"/changesets/{body['id']}/confirm"), 200)
    assert confirmed["status"] == "confirmed"
    assert await exceptions(maker) == 1


async def test_busy_over_an_in_progress_block_does_not_block_confirm(
    cal, cipher, client, maker, who
):
    body = await staged(client, maker, who.user_id, cipher)
    cal.busy = [(at(0, "00:00"), NOW)]  # only the past: future blocks untouched
    await ok(client.post(f"/changesets/{body['id']}/confirm"), 200)


async def test_transient_on_confirm_is_503_and_applies_nothing(cal, cipher, client, maker, who):
    body = await staged(client, maker, who.user_id, cipher)
    cal.freebusy = Reply(status=500, json={})
    resp = await client.post(f"/changesets/{body['id']}/confirm")
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "gcal_unavailable"
    assert_conforms("confirmChangeset", 503, resp.json())
    assert await exceptions(maker) == 0
    assert await status(maker, body["id"]) == "pending"


@pytest.mark.parametrize("how", ["stored", "invalid_grant"])
async def test_revoked_on_confirm_is_409_and_applies_nothing(
    how, google, cal, cipher, client, maker, who
):
    body = await staged(client, maker, who.user_id, cipher)
    if how == "stored":
        async with maker() as s, s.begin():
            await s.execute(text("UPDATE google_connections SET status = 'revoked'"))
    else:
        google.refresh = Reply(status=400, json={"error": "invalid_grant"})
    resp = await client.post(f"/changesets/{body['id']}/confirm")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "gcal_reconnect_required"
    assert await exceptions(maker) == 0
    assert await status(maker, body["id"]) == "pending"


def without_outbox(changeset: dict[str, Any]) -> dict[str, Any]:
    plan = changeset["plan"]
    blocks = [
        {k: v for k, v in b.items() if k not in ("gcal_status", "gcal_event_id")}
        for b in plan["blocks"]
    ]
    return changeset | {"plan": plan | {"blocks": blocks}}


async def test_confirm_replay_and_planless_changesets_read_no_busy(
    google, cal, cipher, client, maker, who
):
    body = await staged(client, maker, who.user_id, cipher)
    first = await ok(client.post(f"/changesets/{body['id']}/confirm"), 200)
    cal.freebusy = Reply(status=500, json={})  # Google down: an idempotent replay still works
    replay = await ok(client.post(f"/changesets/{body['id']}/confirm"), 200)
    # The confirm kicked a write-back drain (T4.3a): the replay shows the blocks' current
    # outbox state, which may have moved on. Everything else is the same changeset.
    assert without_outbox(replay) == without_outbox(first)
    unknown = await client.post(f"/changesets/{uuid.uuid4()}/confirm")
    assert unknown.status_code == 404  # not a gcal error: nothing to solve


async def test_week_rollover_reads_both_weeks_and_restages_against_fresh_busy(
    cal, cipher, client, clock, maker, who
):
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cs = (await ok(client.patch(f"/projects/{WORKOUT}", json={"weekly_target_hours": 2.0})))["id"]
    clock.current = at(7, "06:00")  # next Monday 06:00 local: the proposal's week has ended
    next_tuesday = (at(8, "00:00"), at(9, "00:00"))
    cal.busy = [next_tuesday]
    stale = await client.post(f"/changesets/{cs}/confirm")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "changeset_stale"
    # One read spanning the proposal's week and the current one.
    assert cal.freebusy_bodies[-1]["timeMin"] == "2026-11-16T08:00:00Z"
    assert cal.freebusy_bodies[-1]["timeMax"] == "2026-11-30T08:00:00Z"
    fresh = stale.json()["error"]["details"]["changeset_id"]
    plan_id = (
        await rows(maker, "SELECT plan_id FROM changesets WHERE id = :c", c=uuid.UUID(fresh))
    )[0][0]
    assert await solver_busy(maker, str(plan_id)) == [
        {"start": z(next_tuesday[0]), "end": z(next_tuesday[1])}
    ]
    confirmed = await ok(client.post(f"/changesets/{fresh}/confirm"), 200)
    assert confirmed["plan"]["week_start"] == "2026-11-23"
    assert not any(overlaps(b["span"], *next_tuesday) for b in confirmed["plan"]["blocks"])


async def test_stale_restage_with_busy_never_holds_the_lock_before_restage(
    cal, cipher, client, clock, maker, who, monkeypatch
):
    """Deadlock regression (T4.2 round 1): with Google connected, nothing may take the
    per-user lock between the stale confirm's rollback and `_restage` (which locks first).
    A concurrent request needing the lock in that gap (here a reject in another session)
    must complete. Guarded by a timeout so a regression fails instead of hanging."""
    from app.api import changesets as module

    body = await staged(client, maker, who.user_id, cipher)
    clock.current = at(0, "08:30")  # Monday's first block started: confirm goes stale
    original = module._restage

    async def racing(*args: Any, **kwargs: Any) -> Any:
        from tests.db.conftest import reject

        await asyncio.wait_for(reject(maker, who.user_id, uuid.UUID(body["id"]), clock.current), 10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "_restage", racing)
    resp = await asyncio.wait_for(client.post(f"/changesets/{body['id']}/confirm"), 20)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == {
        "code": "changeset_not_pending",
        "message": resp.json()["error"]["message"],
        "details": {"status": "rejected"},
    }
    assert len(cal.freebusy_bodies) == 2  # staging + confirm: busy was read on this path

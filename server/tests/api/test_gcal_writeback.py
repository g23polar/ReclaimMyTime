"""T4.3a write-back outbox through the API: `POST /gcal/sync` and the confirm kick.

Recorded HTTP only (tests.gcal.calendar_fakes.FakeCalendar keeps an in-memory event store with
Google's id semantics). The `world` user is onboarded at T0 (every block `pending`), clock at
NOW = Tue 10:30 local. Concurrency tests are timeout-guarded: a held lock must fail, not hang.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.api import gcal as gcal_routes
from app.db.outbox import event_id_for
from tests.api.conftest import NOW, live_blocks
from tests.api.contract import assert_conforms
from tests.api.test_busy_routes import WEDNESDAY_OFF, ok
from tests.db.conftest import WEEK, requires_db, rows
from tests.gcal.calendar_fakes import FakeCalendar, seed_connection, z
from tests.gcal.fakes import Reply

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]

CAL1 = "cal1@group.calendar.google.com"
CAL2 = "cal2@group.calendar.google.com"


async def sync(client: Any, status: int = 200) -> Any:
    resp = await client.post("/gcal/sync")
    assert resp.status_code == status, resp.text
    assert_conforms("syncGcal", status, resp.json())
    return resp.json()


async def settle(app: FastAPI) -> None:
    registry = getattr(app.state, "gcal_outbox", None)
    if registry is not None:
        await asyncio.wait_for(registry.idle(), 20)


async def outbox_rows(maker: Any, uid: uuid.UUID) -> list[Any]:
    """(lineage, span start, gcal_status, gcal_event_id, gcal_etag) of the live blocks."""
    return [
        (b.lineage_id, b.span.lower, b.gcal_status, b.gcal_event_id, b.gcal_etag)
        for b in await live_blocks(maker, uid)
    ]


def expected_id(lineage_id: uuid.UUID) -> str:
    return event_id_for(lineage_id, WEEK)


@pytest.fixture
async def connected(maker: Any, who: Any, cipher: Any) -> uuid.UUID:
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    return who.user_id


# ---------------------------------------------------------------- create / update / delete


async def test_sync_writes_each_live_block_once_keyed_by_lineage(
    connected, client, maker, cal: FakeCalendar
):
    body = await sync(client)
    blocks = await live_blocks(maker, connected)
    assert body == {"pending_changeset": None, "outbox": {"pending": 0, "synced": len(blocks)}}
    events = cal.calendar_events(CAL1)
    assert set(events) == {expected_id(b.lineage_id) for b in blocks}
    names = dict(await rows(maker, "SELECT id, name FROM projects"))
    for b in blocks:
        ev = events[expected_id(b.lineage_id)]
        assert ev["summary"] == names[b.project_id]
        assert ev["description"] == "Scheduled by ReclaimMyTime"
        assert ev["start"] == {"dateTime": z(b.span.lower)}
        assert ev["end"] == {"dateTime": z(b.span.upper)}
        assert ev["extendedProperties"]["private"] == {
            "lineage_id": str(b.lineage_id),
            "block_id": str(b.id),
        }
        assert (b.gcal_status, b.gcal_event_id, b.gcal_etag) == (
            "synced",
            expected_id(b.lineage_id),
            ev["etag"],
        )
    # Idempotent: nothing pending → no Google event call at all.
    calls = len(cal.event_calls)
    assert (await sync(client))["outbox"]["pending"] == 0
    assert len(cal.event_calls) == calls


async def test_confirm_moves_update_and_removed_lineages_are_deleted(
    app, connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    before = {b.lineage_id: b for b in await live_blocks(maker, connected)}
    cal.requests.clear()

    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
    await settle(app)

    after = {b.lineage_id: b for b in await live_blocks(maker, connected)}
    moved = {lid for lid in before.keys() & after.keys() if before[lid].span != after[lid].span}
    removed = before.keys() - after.keys()
    added = after.keys() - before.keys()
    assert moved or removed, "the exception must change the plan for this test to mean anything"

    calls = cal.event_calls
    # A move is an update of the lineage's own event (update first: it is known to exist).
    for lid in moved:
        assert ("PUT", expected_id(lid)) in calls
        assert ("POST", expected_id(lid)) not in calls
    for lid in removed:
        assert ("DELETE", expected_id(lid)) in calls
    for lid in added:
        assert ("POST", expected_id(lid)) in calls
    unchanged = {lid for lid in before.keys() & after.keys() if lid not in moved}
    assert not {eid for _, eid in calls} & {expected_id(lid) for lid in unchanged}

    live = cal.calendar_events(CAL1)
    assert set(live) == {expected_id(lid) for lid in after}
    for lid, b in after.items():
        assert live[expected_id(lid)]["start"] == {"dateTime": z(b.span.lower)}
        assert b.gcal_status == "synced"
    # Superseded rows no longer name a deleted event.
    for lid in removed:
        named = await rows(
            maker, "SELECT count(*) FROM blocks WHERE gcal_event_id = :e", e=expected_id(lid)
        )
        assert named == [(0,)]


async def test_create_409_duplicate_falls_through_to_update(
    connected, client, maker, cal: FakeCalendar
):
    """The deterministic id already exists (an earlier attempt whose result was lost, or an
    event the user deleted in Google): the insert's 409 becomes an update that revives it."""
    target = (await live_blocks(maker, connected))[-1]
    eid = expected_id(target.lineage_id)
    cal.events[(CAL1, eid)] = {"id": eid, "status": "cancelled", "summary": "stale", "etag": "x"}

    await sync(client)
    assert [c for c in cal.event_calls if c[1] == eid] == [("POST", eid), ("PUT", eid)]
    ev = cal.events[(CAL1, eid)]
    assert ev["status"] == "confirmed"
    assert ev["start"] == {"dateTime": z(target.span.lower)}
    row = next(r for r in await outbox_rows(maker, connected) if r[0] == target.lineage_id)
    assert row[2:4] == ("synced", eid)


async def test_calendar_404_recreates_the_calendar_and_rewrites_what_has_not_ended(
    connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    blocks = await live_blocks(maker, connected)
    future = [b for b in blocks if b.span.upper > NOW]
    ended = [b for b in blocks if b.span.upper <= NOW]
    assert future and ended
    # The user deleted the app calendar in Google; one future block moves (pending again).
    cal.gone_calendars.add(CAL1)
    await rows_exec(maker, "UPDATE blocks SET gcal_status = 'pending' WHERE id = :b", future[0].id)

    body = await sync(client)
    assert body["outbox"] == {"pending": 0, "synced": len(blocks)}
    assert cal.created == 2  # first sync + the recreation
    stored = await rows(maker, "SELECT app_calendar_id FROM google_connections")
    assert stored == [(CAL2,)]
    # Everything that has not ended is in the new calendar; ended blocks are never rewritten.
    assert set(cal.calendar_events(CAL2)) == {expected_id(b.lineage_id) for b in future}


async def rows_exec(maker: Any, sql: str, block_id: uuid.UUID) -> None:
    from sqlalchemy import text

    async with maker() as s, s.begin():
        await s.execute(text(sql), {"b": block_id})


# ---------------------------------------------------------------- failures


async def test_partial_failure_keeps_synced_rows_and_a_later_sync_completes(
    connected, client, maker, cal: FakeCalendar
):
    """M4 acceptance: Google fails midway → written rows stay synced, the rest stay pending
    (retryable), and the next /gcal/sync finishes without rewriting what was written."""
    blocks = await live_blocks(maker, connected)
    assert len(blocks) > 3
    written: list[str] = []

    def fault(method: str, event_id: str) -> Reply | None:
        if len(written) >= 3:
            return Reply(status=503, json={"error": {"code": 503}})
        written.append(event_id)
        return None

    cal.event_fault = fault
    resp = await sync(client, 503)
    assert resp["error"]["code"] == "gcal_unavailable"
    states = await outbox_rows(maker, connected)
    assert sorted(r[2] for r in states).count("synced") == 3
    assert {r[3] for r in states if r[2] == "synced"} == set(written)
    assert all(r[3] is None for r in states if r[2] == "pending")

    cal.event_fault = None
    cal.requests.clear()
    body = await sync(client)
    assert body["outbox"] == {"pending": 0, "synced": len(blocks)}
    assert not set(written) & {eid for _, eid in cal.event_calls}  # synced rows untouched
    assert set(cal.calendar_events(CAL1)) == {expected_id(b.lineage_id) for b in blocks}


async def test_a_rejected_event_is_skipped_and_stays_pending(
    connected, client, maker, cal: FakeCalendar
):
    poison = expected_id((await live_blocks(maker, connected))[0].lineage_id)
    cal.event_fault = lambda m, eid: Reply(status=400, json={}) if eid == poison else None
    body = await sync(client)
    assert body["outbox"]["pending"] == 1
    pending = [r for r in await outbox_rows(maker, connected) if r[2] == "pending"]
    assert [expected_id(r[0]) for r in pending] == [poison]


async def test_stored_revoked_connection_is_409_and_nothing_is_written(
    client, maker, who, cipher, cal: FakeCalendar
):
    await seed_connection(maker, who.user_id, cipher, status="revoked", now=NOW)
    resp = await sync(client, 409)
    assert resp["error"]["code"] == "gcal_reconnect_required"
    assert cal.event_calls == []
    assert {r[2] for r in await outbox_rows(maker, who.user_id)} == {"pending"}


async def test_invalid_grant_while_minting_is_409_marks_revoked_and_keeps_rows_pending(
    connected, client, maker, google, cal: FakeCalendar
):
    """Marking the grant revoked takes the per-user lock: the drain must not hold it (or any
    session) while minting, or this would deadlock — hence the timeout guard."""
    google.refresh = Reply(status=400, json={"error": "invalid_grant"})
    resp = await asyncio.wait_for(sync(client, 409), 20)
    assert resp["error"]["code"] == "gcal_reconnect_required"
    assert await rows(maker, "SELECT status::text FROM google_connections") == [("revoked",)]
    assert cal.event_calls == []
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"pending"}


async def test_token_endpoint_down_is_503(connected, client, google, maker):
    google.refresh = Reply(status=503, json={})
    resp = await sync(client, 503)
    assert resp["error"]["code"] == "gcal_unavailable"
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"pending"}


# ---------------------------------------------------------------- the response


async def test_never_connected_is_200_with_an_empty_outbox_and_the_pending_changeset(
    client, cal: FakeCalendar
):
    assert await sync(client) == {"pending_changeset": None, "outbox": {"pending": 0, "synced": 0}}
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    body = await sync(client)
    assert body["pending_changeset"] == staged  # O2/G3: the banner survives a relaunch
    assert body["pending_changeset"]["reason"] == "user_edit"
    assert cal.requests == []


async def test_sync_returns_the_pending_changeset_when_connected(connected, client):
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    body = await sync(client)
    assert body["pending_changeset"]["id"] == staged["id"]
    assert body["pending_changeset"]["status"] == "pending"


async def test_sync_requires_x_timezone(connected, client):
    resp = await client.post("/gcal/sync", headers={"X-Timezone": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "timezone_invalid"


async def test_project_names_and_event_bodies_are_never_logged(connected, client, maker, caplog):
    caplog.set_level(logging.DEBUG)
    await sync(client)
    # (The world's deadline project is named "P": too short to search for meaningfully.)
    names = [n for (n,) in await rows(maker, "SELECT name FROM projects") if len(n) > 2]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert names and not any(n in text for n in names)
    assert "Scheduled by ReclaimMyTime" not in text
    assert CAL1 not in text


# ---------------------------------------------------------------- the kick and concurrency


async def test_confirm_kicks_a_background_drain_without_waiting_for_it(
    app, connected, client, maker, cal: FakeCalendar
):
    gate = cal.event_gate = asyncio.Event()
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    # The drain is parked inside Google; confirm must answer anyway.
    resp = await asyncio.wait_for(client.post(f"/changesets/{staged['id']}/confirm"), 10)
    assert resp.status_code == 200, resp.text
    await asyncio.wait_for(cal.event_started.wait(), 10)
    assert app.state.gcal_outbox.running(connected)
    assert "pending" in {r[2] for r in await outbox_rows(maker, connected)}

    gate.set()
    await settle(app)
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"synced"}


async def test_a_failing_drain_never_fails_confirm(
    app, connected, client, maker, cal: FakeCalendar
):
    cal.event_fault = lambda m, eid: Reply(status=503, json={})
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
    await settle(app)
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"pending"}


async def test_one_drain_per_user_at_a_time(app, connected, client, maker, cal: FakeCalendar):
    gate = cal.event_gate = asyncio.Event()
    registry = app.state.gcal_outbox = gcal_outbox(app)
    first = registry.kick(connected)
    await asyncio.wait_for(cal.event_started.wait(), 10)
    assert registry.kick(connected) is first  # joins the running drain (and re-runs it)
    pending_sync = asyncio.create_task(client.post("/gcal/sync"))
    await asyncio.sleep(0.05)
    assert registry.kick(connected) is first
    gate.set()
    resp = await asyncio.wait_for(pending_sync, 20)
    assert resp.status_code == 200, resp.text
    await settle(app)
    inserts = [eid for m, eid in cal.event_calls if m == "POST"]
    assert len(inserts) == len(set(inserts))  # no event written twice by racing drains
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"synced"}


def gcal_outbox(app: FastAPI) -> Any:
    from app.api.outbox import get_outbox

    return get_outbox(app)


async def test_sync_returns_within_its_budget_and_the_drain_finishes_later(
    app, connected, client, maker, cal: FakeCalendar, monkeypatch
):
    monkeypatch.setattr(gcal_routes, "SYNC_BUDGET_S", 0.2)
    gate = cal.event_gate = asyncio.Event()
    body = await asyncio.wait_for(sync(client), 10)
    total = len(await live_blocks(maker, connected))
    assert body["outbox"] == {"pending": total, "synced": 0}
    gate.set()
    await settle(app)
    assert (await sync(client))["outbox"] == {"pending": 0, "synced": total}


async def test_orphans_of_ended_blocks_are_never_deleted(connected, client, maker):
    """claim_deletes only offers events of blocks that have not ended (Principle 4)."""
    from sqlalchemy import text

    from app.db.outbox import claim_deletes

    await sync(client)
    blocks = await live_blocks(maker, connected)
    async with maker() as s, s.begin():  # every lineage leaves the live plan
        await s.execute(text("UPDATE week_plans SET status = 'superseded' WHERE status = 'live'"))
    async with maker() as s, s.begin():
        orphans = await claim_deletes(s, connected, NOW)
    assert orphans == sorted(expected_id(b.lineage_id) for b in blocks if b.span.upper > NOW)
    assert any(b.span.upper <= NOW for b in blocks)

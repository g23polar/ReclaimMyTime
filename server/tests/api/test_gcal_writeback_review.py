"""T4.3a review round 1: unrecorded inserts are cleaned up, a replacement calendar is never
stored before its repopulation is durable, and a rename refreshes event titles. Recorded HTTP
only; concurrency is timeout-guarded (see test_gcal_writeback.py for the shared world)."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any

import pytest

from app.db import outbox
from app.gcal.app_calendar import AppCalendarService
from tests.api.conftest import NOW, live_blocks
from tests.api.test_busy_routes import ok
from tests.api.test_gcal_writeback import (
    CAL1,
    CAL2,
    expected_id,
    outbox_rows,
    rows_exec,
    settle,
    sync,
)
from tests.db.conftest import WORKOUT, requires_db, rows
from tests.gcal.calendar_fakes import FakeCalendar, seed_connection
from tests.gcal.fakes import Reply

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]


@pytest.fixture
async def connected(maker: Any, who: Any, cipher: Any) -> uuid.UUID:
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    return who.user_id


async def stored_calendar(maker: Any) -> list[Any]:
    return await rows(maker, "SELECT app_calendar_id FROM google_connections")


# ---------------------------------------------------------------- fix 1: unrecorded inserts


async def test_an_insert_google_accepted_but_we_never_recorded_is_deleted_when_its_lineage_leaves(
    app, connected, client, maker, cal: FakeCalendar, monkeypatch
):
    blocks = await live_blocks(maker, connected)
    target = next(b for b in blocks if b.project_id == WORKOUT and b.span.upper > NOW)
    eid = expected_id(target.lineage_id)
    real = outbox.record_synced

    async def lost(
        tx: Any, user_id: Any, block_id: Any, event_id: str, etag: str, title: str
    ) -> bool:
        if block_id == target.id:
            raise RuntimeError("process died after Google accepted the insert")
        return await real(tx, user_id, block_id, event_id, etag, title)

    monkeypatch.setattr(outbox, "record_synced", lost)
    await sync(client, 503)
    assert eid in cal.calendar_events(CAL1)  # Google has it ...
    row = next(r for r in await outbox_rows(maker, connected) if r[0] == target.lineage_id)
    assert row[2:4] == ("pending", None)  # ... we never recorded it
    monkeypatch.setattr(outbox, "record_synced", real)

    # The lineage leaves the live plan (its project is archived) while still unrecorded.
    staged = await ok(client.delete(f"/projects/{WORKOUT}"))
    await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
    await settle(app)
    assert ("DELETE", eid) in cal.event_calls
    assert eid not in cal.calendar_events(CAL1)
    gone = {
        expected_id(b.lineage_id)
        for b in blocks
        if b.project_id == WORKOUT and b.span.upper > NOW  # ended events are never touched
    }
    assert not gone & set(cal.calendar_events(CAL1))

    # Settled: never offered again (a 404/410 for the never-inserted ones counted as done).
    cal.requests.clear()
    assert (await sync(client))["outbox"]["pending"] == 0
    assert [c for c in cal.event_calls if c[0] == "DELETE"] == []


# ---------------------------------------------------------------- fix 2: durable re-pend


async def _calendar_deleted_in_google(maker: Any, uid: uuid.UUID, cal: FakeCalendar) -> list[Any]:
    """Everything synced to CAL1, then CAL1 is deleted in Google and one future block goes
    pending (so the next drain hits the 404). Returns the blocks that have not ended."""
    blocks = await live_blocks(maker, uid)
    future = [b for b in blocks if b.span.upper > NOW]
    assert future and len(future) < len(blocks)
    cal.gone_calendars.add(CAL1)
    await rows_exec(maker, "UPDATE blocks SET gcal_status = 'pending' WHERE id = :b", future[0].id)
    return future


def _states(rows_: list[Any], ids: set[uuid.UUID]) -> set[str]:
    return {r[2] for r in rows_ if r[0] in ids}


async def test_recreation_interrupted_after_the_new_calendar_is_stored_loses_nothing(
    connected, client, maker, cal: FakeCalendar, monkeypatch
):
    """The process dies right after the replacement id is committed. The re-pend was
    committed first, so the next drain repopulates the new calendar."""
    await sync(client)
    future = await _calendar_deleted_in_google(maker, connected, cal)
    real = AppCalendarService.ensure_app_calendar

    async def dies_after_store(self: Any, user_id: Any, *, missing: str | None = None) -> str:
        calendar_id = await real(self, user_id, missing=missing)
        if missing is not None:
            raise RuntimeError("process died after storing the new calendar")
        return calendar_id

    monkeypatch.setattr(AppCalendarService, "ensure_app_calendar", dies_after_store)
    await sync(client, 503)
    assert await stored_calendar(maker) == [(CAL2,)]
    state = await outbox_rows(maker, connected)
    future_lineages = {b.lineage_id for b in future}
    assert _states(state, future_lineages) == {"pending"}
    assert _states(state, {r[0] for r in state} - future_lineages) == {"synced"}  # ended

    monkeypatch.setattr(AppCalendarService, "ensure_app_calendar", real)
    body = await sync(client)
    assert body["outbox"]["pending"] == 0
    assert set(cal.calendar_events(CAL2)) == {expected_id(b.lineage_id) for b in future}


async def test_recreation_failing_at_google_leaves_rows_pending_and_a_later_sync_completes(
    connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    future = await _calendar_deleted_in_google(maker, connected, cal)
    cal.create = Reply(status=503, json={})
    await sync(client, 503)
    assert await stored_calendar(maker) == [(None,)]  # cleared, nothing new stored
    state = await outbox_rows(maker, connected)
    assert _states(state, {b.lineage_id for b in future}) == {"pending"}

    cal.create = None
    assert (await sync(client))["outbox"]["pending"] == 0
    assert await stored_calendar(maker) == [(CAL2,)]
    assert set(cal.calendar_events(CAL2)) == {expected_id(b.lineage_id) for b in future}


# ---------------------------------------------------------------- fix 4: rename refresh


async def test_a_rename_re_pends_unended_blocks_and_the_drain_retitles_only_those(
    app, connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    blocks = await live_blocks(maker, connected)
    by_project: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for b in blocks:
        by_project[b.project_id].append(b)
    pid, mine = next(
        (p, bs)
        for p, bs in by_project.items()
        if any(b.span.upper > NOW for b in bs) and any(b.span.upper <= NOW for b in bs)
    )
    future = {b.lineage_id for b in mine if b.span.upper > NOW}
    ended = {b.lineage_id for b in mine if b.span.upper <= NOW}
    old_titles = {lid: cal.events[(CAL1, expected_id(lid))]["summary"] for lid in ended}
    others = {b.lineage_id for b in blocks} - future - ended

    gate = cal.event_gate = asyncio.Event()  # hold the kicked drain to observe the outbox
    resp = await asyncio.wait_for(client.patch(f"/projects/{pid}", json={"name": "Renamed"}), 10)
    assert resp.status_code == 200, resp.text
    state = await outbox_rows(maker, connected)
    assert _states(state, future) == {"pending"}
    assert _states(state, ended | others) == {"synced"}

    cal.requests.clear()
    gate.set()
    await settle(app)
    assert {eid for _, eid in cal.event_calls} == {expected_id(lid) for lid in future}
    for lid in future:
        assert cal.events[(CAL1, expected_id(lid))]["summary"] == "Renamed"
    for lid in ended:  # past events are immutable: they keep the old title
        assert cal.events[(CAL1, expected_id(lid))]["summary"] == old_titles[lid]
    assert {r[2] for r in await outbox_rows(maker, connected)} == {"synced"}


async def test_a_rename_inside_a_changeset_retitles_on_confirm(
    app, connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    staged = await ok(
        client.patch(f"/projects/{WORKOUT}", json={"name": "Gym", "weekly_target_hours": 4.0})
    )
    await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
    await settle(app)
    mine = [b for b in await live_blocks(maker, connected) if b.project_id == WORKOUT]
    assert any(b.span.upper > NOW for b in mine)
    for b in mine:
        if b.span.upper > NOW:
            assert cal.events[(CAL1, expected_id(b.lineage_id))]["summary"] == "Gym"
        assert b.gcal_status == "synced"


async def test_renaming_to_the_same_name_writes_nothing(
    app, connected, client, maker, cal: FakeCalendar
):
    await sync(client)
    name = (await rows(maker, "SELECT name FROM projects WHERE id = :p", p=WORKOUT))[0][0]
    cal.requests.clear()
    resp = await client.patch(f"/projects/{WORKOUT}", json={"name": name})
    assert resp.status_code == 200, resp.text
    await settle(app)
    assert cal.event_calls == []


# ---------------------------------------------------------------- round 2


async def test_a_rename_during_the_google_call_is_not_recorded_as_synced(
    app, connected, client, maker, cal: FakeCalendar, monkeypatch
):
    """The drain claimed the row with the old name; the rename commits while the Google call
    is in flight. The CAS sees the current name differs: the row stays pending (with the new
    event id/etag) and the follow-up pass writes the new title."""
    await sync(client)
    target = next(
        b
        for b in await live_blocks(maker, connected)
        if b.project_id == WORKOUT and b.span.upper > NOW
    )
    eid = expected_id(target.lineage_id)
    await rows_exec(maker, "UPDATE blocks SET gcal_status = 'pending' WHERE id = :b", target.id)
    real = outbox.record_synced
    recorded: list[tuple[str, bool]] = []

    async def spy(tx: Any, user_id: Any, block_id: Any, event_id: str, etag: str, title: str):
        ok_ = await real(tx, user_id, block_id, event_id, etag, title)
        if block_id == target.id:
            recorded.append((title, ok_))
        return ok_

    monkeypatch.setattr(outbox, "record_synced", spy)
    gate = cal.event_gate = asyncio.Event()
    cal.event_started.clear()
    task = outbox_registry(app).kick(connected)
    await asyncio.wait_for(cal.event_started.wait(), 10)  # the old title is on the wire
    resp = await asyncio.wait_for(client.patch(f"/projects/{WORKOUT}", json={"name": "Gym"}), 10)
    assert resp.status_code == 200, resp.text
    gate.set()
    await asyncio.wait_for(task, 20)
    await settle(app)

    old_title, first_ok = recorded[0]
    assert old_title != "Gym" and first_ok is False  # not recorded as synced
    assert recorded[-1] == ("Gym", True)
    assert cal.events[(CAL1, eid)]["summary"] == "Gym"
    row = next(r for r in await outbox_rows(maker, connected) if r[0] == target.lineage_id)
    assert row[2:4] == ("synced", eid)


def outbox_registry(app: Any) -> Any:
    from app.api.outbox import get_outbox

    return get_outbox(app)


async def test_a_delayed_retry_never_rewrites_an_event_that_has_ended(
    connected, client, clock, maker, cal: FakeCalendar
):
    """A rename re-pends an unended block; Google is down until after it ends. Its event
    already exists, so the retry settles it `synced` with no Google call (Principle 4)."""
    await sync(client)
    target = next(
        b
        for b in await live_blocks(maker, connected)
        if b.project_id == WORKOUT and b.span.upper > NOW
    )
    eid = expected_id(target.lineage_id)
    old_title = cal.events[(CAL1, eid)]["summary"]
    etag = cal.events[(CAL1, eid)]["etag"]

    cal.event_fault = lambda m, e: Reply(status=503, json={})
    resp = await client.patch(f"/projects/{WORKOUT}", json={"name": "Gym"})
    assert resp.status_code == 200, resp.text
    await sync(client, 503)
    row = next(r for r in await outbox_rows(maker, connected) if r[0] == target.lineage_id)
    assert row[2] == "pending"

    cal.event_fault = None
    cal.requests.clear()
    clock.current = target.span.upper  # the block has ended
    await sync(client)
    assert [c for c in cal.event_calls if c[1] == eid] == []
    assert cal.events[(CAL1, eid)]["summary"] == old_title
    row = next(r for r in await outbox_rows(maker, connected) if r[0] == target.lineage_id)
    assert row[2:5] == ("synced", eid, etag)

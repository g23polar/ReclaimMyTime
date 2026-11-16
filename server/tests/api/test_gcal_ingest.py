"""T4.3b through the API: `POST /gcal/sync` ingests edits made to our events inside Google
(hybrid rule) and runs the drift check. Recorded HTTP only (tests.gcal.calendar_fakes).

The `stable` world: prefs + Workout (6 h/week, 60-min blocks) + Portfolio, confirmed at T0,
then re-solved and confirmed at NOW (Tue 10:30 local), so a re-solve at NOW reproduces the
live plan (otherwise every re-solve shifts the deadline project and nothing is ever case (i)).
Connected and synced once. Future blocks: Wed 08:00 Workout, Wed 09:15–10:35 Portfolio,
Thu/Fri/Sat 08:00 Workout. Windows 08:00–20:00, buffer 15 min.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.api import gcal_sync
from app.db.outbox import event_id_for, ingest_candidates
from app.db.write_paths import propose_changeset
from tests.api.conftest import NOW, live_blocks
from tests.api.contract import assert_conforms
from tests.api.test_busy_routes import WEDNESDAY_OFF, ok
from tests.db.conftest import (
    PORTFOLIO,
    WEEK,
    WORKOUT,
    at,
    confirm,
    deadline_project,
    exception_added,
    prefs,
    propose,
    requires_db,
    rows,
    workout,
)
from tests.gcal.calendar_fakes import FakeCalendar, seed_connection, z
from tests.gcal.fakes import Reply

pytestmark = [requires_db, pytest.mark.usefixtures("gcal")]

CAL1 = "cal1@group.calendar.google.com"
CAL2 = "cal2@group.calendar.google.com"


async def sync(client: Any, status: int = 200) -> Any:
    resp = await asyncio.wait_for(client.post("/gcal/sync"), 30)
    assert resp.status_code == status, resp.text
    assert_conforms("syncGcal", status, resp.json())
    return resp.json()


async def settle(app: FastAPI) -> None:
    registry = getattr(app.state, "gcal_outbox", None)
    if registry is not None:
        await asyncio.wait_for(registry.idle(), 20)


def eid(block: Any) -> str:
    return event_id_for(block.lineage_id, WEEK)


async def block_at(maker: Any, uid: uuid.UUID, day: int, hhmm: str) -> Any:
    return next(b for b in await live_blocks(maker, uid) if b.span.lower == at(day, hhmm))


async def spans(maker: Any, uid: uuid.UUID) -> list[tuple[Any, Any, Any]]:
    return [(b.project_id, b.span.lower, b.span.upper) for b in await live_blocks(maker, uid)]


async def live_plan_id(maker: Any) -> Any:
    return (await rows(maker, "SELECT id FROM week_plans WHERE status = 'live'"))[0][0]


async def changesets(maker: Any) -> list[tuple[str, str, int]]:
    return [
        tuple(r)
        for r in await rows(
            maker,
            "SELECT reason::text, status::text, jsonb_array_length(staged) FROM changesets"
            " ORDER BY created_at, id",
        )
    ]


async def make_world(
    maker: Any,
    uid: uuid.UUID,
    cipher: Any,
    clock: Any,
    client: Any,
    *,
    extra: list[Any] | None = None,
    projects: list[Any] | None = None,
) -> None:
    projects = projects if projects is not None else [workout(hours=6.0), deadline_project()]
    cs = await propose(maker, uid, [prefs(), *projects])
    await confirm(maker, uid, cs)
    clock.current = NOW
    async with maker() as s, s.begin():
        if extra:
            cs = await propose_changeset(s, uid, extra_staged(extra), now=NOW)
        else:
            cs = await propose_changeset(s, uid, [], now=NOW, source="system", reason="drift")
    await confirm(maker, uid, cs, now=NOW)
    await seed_connection(maker, uid, cipher, now=NOW)
    body = await sync(client)
    assert body["pending_changeset"] is None and body["outbox"]["pending"] == 0


def extra_staged(items: list[Any]) -> list[Any]:
    from tests.db.conftest import staged

    return staged(*items)


@pytest.fixture
async def stable(maker, who, cipher, clock, client) -> uuid.UUID:
    await make_world(maker, who.user_id, cipher, clock, client)
    assert [(p, z(s)) for p, s, _ in await spans(maker, who.user_id) if s > NOW] == [
        (WORKOUT, z(at(2, "08:00"))),
        (PORTFOLIO, z(at(2, "09:15"))),
        (WORKOUT, z(at(3, "08:00"))),
        (WORKOUT, z(at(4, "08:00"))),
        (WORKOUT, z(at(5, "08:00"))),
    ]
    return who.user_id


# ---------------------------------------------------------------- detection


async def test_unchanged_etags_change_nothing(stable, client, maker, cal: FakeCalendar):
    before = await live_blocks(maker, stable)
    writes, reads = len(cal.event_calls), len(cal.event_reads)
    body = await sync(client)
    assert body["pending_changeset"] is None
    assert [b.id for b in await live_blocks(maker, stable)] == [b.id for b in before]
    assert len(cal.event_calls) == writes  # nothing rewritten
    assert [r[0] for r in cal.event_reads[reads:]] == [""]  # one listing, no events.get
    listing = cal.event_reads[-1][1]
    assert listing["showDeleted"] == "true" and listing["singleEvents"] == "true"
    assert "summary" not in listing["fields"] and "description" not in listing["fields"]
    assert await changesets(maker) == [("agent", "confirmed", 3), ("drift", "confirmed", 0)]


async def test_title_only_edit_is_ignored(stable, client, maker, cal: FakeCalendar):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_retitle(CAL1, eid(wed), "Renamed by the user")
    plan = await live_plan_id(maker)
    assert (await sync(client))["pending_changeset"] is None
    assert await live_plan_id(maker) == plan
    after = await block_at(maker, stable, 2, "08:00")
    assert (after.gcal_status, after.gcal_etag) == ("synced", wed.gcal_etag)


async def test_edits_to_started_blocks_and_moves_into_the_past_are_ignored(
    stable, client, maker, cal: FakeCalendar, clock
):
    ended = next(b for b in await live_blocks(maker, stable) if b.span.upper <= NOW)
    cal.user_move(CAL1, eid(ended), at(3, "14:00"), at(3, "15:00"))
    thu = await block_at(maker, stable, 3, "08:00")
    cal.user_move(CAL1, eid(thu), at(1, "08:00"), at(1, "09:00"))  # into the past
    plan = await live_plan_id(maker)
    assert (await sync(client))["pending_changeset"] is None
    assert await live_plan_id(maker) == plan

    # In progress: Wednesday's Workout has started; moving its event does nothing.
    clock.current = at(2, "08:30")
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(2, "14:00"), at(2, "15:00"))
    assert (await sync(client))["pending_changeset"] is None
    assert await live_plan_id(maker) == plan


async def test_pending_rows_are_ignored(stable, client, maker, cal: FakeCalendar):
    """Our own write is in flight for a pending row: its event is not judged."""
    thu = await block_at(maker, stable, 3, "08:00")
    async with maker() as s, s.begin():
        await s.execute(
            text("UPDATE blocks SET gcal_status = 'pending' WHERE id = :b"), {"b": thu.id}
        )
    async with maker() as s:
        candidates = await ingest_candidates(s, stable, NOW)
    assert thu.id not in {r.block_id for r in candidates.rows}

    cal.user_move(CAL1, eid(thu), at(3, "10:00"), at(3, "11:00"))
    cal.event_fault = lambda m, e: Reply(status=400, json={}) if e == eid(thu) else None
    plan = await live_plan_id(maker)
    assert (await sync(client))["pending_changeset"] is None
    assert await live_plan_id(maker) == plan
    assert (await block_at(maker, stable, 3, "08:00")).gcal_status == "pending"


async def test_event_moved_out_of_the_week_is_looked_up_and_ignored(
    stable, client, maker, cal: FakeCalendar
):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(8, "10:00"), at(8, "11:00"))  # next week: not listed
    plan = await live_plan_id(maker)
    assert (await sync(client))["pending_changeset"] is None
    assert (eid(wed), {"fields": "id,etag,status,start,end,extendedProperties/private"}) in (
        cal.event_reads
    )
    assert await live_plan_id(maker) == plan


# ---------------------------------------------------------------- moves: the hybrid rule


async def test_move_case_i_goes_live_directly_and_is_not_rewritten(
    app, stable, client, maker, cal: FakeCalendar, caplog
):
    caplog.set_level(logging.DEBUG)
    wed = await block_at(maker, stable, 2, "08:00")
    others = [s for s in await spans(maker, stable) if s[1] != wed.span.lower]
    etag = cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))

    body = await sync(client)
    assert body["pending_changeset"] is None  # nothing for review: nothing else moved
    moved = await block_at(maker, stable, 3, "10:00")
    assert moved.lineage_id == wed.lineage_id
    assert (moved.gcal_status, moved.gcal_event_id, moved.gcal_etag) == ("synced", eid(wed), etag)
    assert [s for s in await spans(maker, stable) if s[1] != at(3, "10:00")] == others
    pins = await rows(
        maker, "SELECT kind::text, project_id, created_from_block_id, span::text FROM block_pins"
    )
    assert [(k, p, c) for k, p, c, _ in pins] == [("pin", WORKOUT, wed.id)]
    trace = await rows(
        maker,
        "SELECT kind::text, source::text, interpretation, changeset_id, payload->>'reason'"
        " FROM mutations WHERE source = 'system' AND changeset_id IS NULL",
    )
    assert sorted(trace) == sorted(
        [
            ("pin_added", "system", "Moved in Google Calendar", None, None),
            ("plan_confirmed", "system", "Moved in Google Calendar", None, "google_edit"),
        ]
    )
    # The event already says what the plan says: the next drain/sync writes nothing.
    await settle(app)
    writes = len(cal.event_calls)
    assert (await sync(client))["pending_changeset"] is None
    await settle(app)
    assert len(cal.event_calls) == writes
    ev = cal.events[(CAL1, eid(wed))]
    assert (ev["start"], ev["etag"]) == ({"dateTime": z(at(3, "10:00"))}, etag)
    # Privacy: no event title or description ever reaches a log line.
    assert not [r for r in caplog.records if "Workout" in r.getMessage()]


async def test_move_case_ii_applies_the_block_and_proposes_the_knock_on(
    app, stable, client, maker, cal: FakeCalendar
):
    """Shrinking Wednesday's Workout to 30 min frees demand: the re-solve adds a block
    elsewhere. The shrunk block goes live; the addition is proposed with nothing staged."""
    wed = await block_at(maker, stable, 2, "08:00")
    etag = cal.user_move(CAL1, eid(wed), at(2, "08:00"), at(2, "08:30"))

    body = await sync(client)
    cs = body["pending_changeset"]
    assert (cs["reason"], cs["staged"]) == ("google_edit", [])
    changes = {e["change"] for e in cs["diff"]["entries"]}
    assert "added" in changes and "removed" not in changes
    live = await block_at(maker, stable, 2, "08:00")
    assert (live.span.upper, live.gcal_status, live.gcal_etag) == (at(2, "08:30"), "synced", etag)
    assert any(
        e["lineage_id"] == str(wed.lineage_id) and e["change"] == "unchanged"
        for e in cs["diff"]["entries"]
    )

    await ok(client.post(f"/changesets/{cs['id']}/confirm"), 200)
    await settle(app)
    kept = await block_at(maker, stable, 2, "08:00")
    assert (kept.span.upper, kept.gcal_etag) == (at(2, "08:30"), etag)  # never rewritten


async def test_move_case_iii_overlap_stages_everything(app, stable, client, maker, cal):
    wed = await block_at(maker, stable, 2, "08:00")
    plan = await live_plan_id(maker)
    cal.user_move(CAL1, eid(wed), at(3, "08:30"), at(3, "09:30"))  # onto Thursday's Workout

    cs = (await sync(client))["pending_changeset"]
    assert cs["reason"] == "google_edit"
    assert [(m["kind"], m["source"], m["payload"]["kind"]) for m in cs["staged"]] == [
        ("pin_added", "system", "pin")
    ]
    assert cs["staged"][0]["payload"]["span"] == {
        "start": z(at(3, "08:30")),
        "end": z(at(3, "09:30")),
    }
    assert await live_plan_id(maker) == plan  # nothing applied
    assert await rows(maker, "SELECT count(*) FROM block_pins") == [(0,)]
    await settle(app)
    # The Google event stays where the user put it.
    assert cal.events[(CAL1, eid(wed))]["start"] == {"dateTime": z(at(3, "08:30"))}


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        ((2, "21:00"), (2, "22:00"), "outside the schedulable window: the pin is not honoured"),
        ((2, "10:45"), (2, "11:45"), "15-min buffer after Portfolio: the one-block plan fails"),
    ],
)
async def test_move_that_cannot_apply_directly_is_staged(
    stable, client, maker, cal, start, end, why
):
    wed = await block_at(maker, stable, 2, "08:00")
    plan = await live_plan_id(maker)
    cal.user_move(CAL1, eid(wed), at(*start), at(*end))
    cs = (await sync(client))["pending_changeset"]
    assert cs["reason"] == "google_edit", why
    assert [m["kind"] for m in cs["staged"]] == ["pin_added"], why
    assert await live_plan_id(maker) == plan, why


# ---------------------------------------------------------------- deletes


@pytest.mark.parametrize("how", ["cancelled", "missing"])
async def test_delete_applies_a_forbid_and_proposes_the_replacement(
    app, stable, client, maker, cal: FakeCalendar, how
):
    wed = await block_at(maker, stable, 2, "08:00")
    writes = len(cal.event_calls)
    if how == "cancelled":
        cal.user_delete(CAL1, eid(wed))
    else:
        cal.user_purge(CAL1, eid(wed))

    cs = (await sync(client))["pending_changeset"]
    if how == "missing":
        assert any(r[0] == eid(wed) for r in cal.event_reads)  # looked up, 404
    assert (cs["reason"], cs["staged"]) == ("google_edit", [])
    assert any(e["change"] == "added" for e in cs["diff"]["entries"])
    assert wed.lineage_id not in {b.lineage_id for b in await live_blocks(maker, stable)}
    pins = await rows(maker, "SELECT kind::text, span::text, created_from_block_id FROM block_pins")
    assert [(k, c) for k, _, c in pins] == [("forbid", wed.id)]
    interp = await rows(
        maker,
        "SELECT DISTINCT interpretation FROM mutations WHERE source = 'system'"
        " AND changeset_id IS NULL",
    )
    assert interp == [("Deleted in Google Calendar",)]
    await settle(app)
    # The removed lineage's event is deleted (410: already cancelled), never rewritten.
    assert [m for m, e in cal.event_calls[writes:] if e == eid(wed)] == ["DELETE"]


async def test_delete_without_room_to_replace_stages_nothing(
    maker, who, cipher, clock, client, cal: FakeCalendar
):
    """Workout only; exceptions leave only Wednesday 08:00–10:50 free, holding two Workouts.
    The deleted one's hour is forbidden for Workout and the rest is too short, so the
    re-solve wants no change and nothing is proposed."""
    await make_world(
        maker,
        who.user_id,
        cipher,
        clock,
        client,
        projects=[workout(hours=6.0)],
        extra=[
            exception_added(NOW, at(2, "08:00"), uuid.uuid4()),
            exception_added(at(2, "10:50"), at(7, "00:00"), uuid.uuid4()),
        ],
    )
    future = [b for b in await live_blocks(maker, who.user_id) if b.span.lower > NOW]
    assert [(b.span.lower, b.span.upper) for b in future] == [
        (at(2, "08:00"), at(2, "09:00")),
        (at(2, "09:15"), at(2, "10:15")),
    ]
    target = future[0]
    cal.user_delete(CAL1, eid(target))

    assert (await sync(client))["pending_changeset"] is None
    assert target.lineage_id not in {b.lineage_id for b in await live_blocks(maker, who.user_id)}
    assert [r[0] for r in await rows(maker, "SELECT kind::text FROM block_pins")] == ["forbid"]
    assert [c for c in await changesets(maker) if c[0] == "google_edit"] == []


# ---------------------------------------------------------------- pending changeset, batching


def workout_minutes(plan: dict[str, Any]) -> int:
    def minutes(span: dict[str, str]) -> int:
        a, b = (datetime.fromisoformat(span[k].replace("Z", "+00:00")) for k in ("start", "end"))
        return int((b - a).total_seconds()) // 60

    return sum(minutes(b["span"]) for b in plan["blocks"] if b["project_id"] == str(WORKOUT))


async def test_direct_edit_while_pending_restages_the_pending_changeset(
    app, stable, client, maker, cal: FakeCalendar
):
    """Review round 1: the knock-on of a direct apply is not swallowed by a pending proposal.
    The user's pending changeset is restaged against the new live plan (same staged list,
    same reason, same fresh busy), so its plan carries the knock-on."""
    pending = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    fri = await block_at(maker, stable, 4, "08:00")
    etag = cal.user_move(CAL1, eid(fri), at(4, "08:00"), at(4, "08:30"))  # case (ii)
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "08:30"), at(3, "09:30"))  # case (iii)

    body = await sync(client)
    fresh = body["pending_changeset"]
    assert fresh["id"] != pending["id"]
    assert (fresh["reason"], fresh["staged"]) == ("user_edit", pending["staged"])
    assert fresh["diff"]["against_plan_id"] == str(await live_plan_id(maker))
    assert sorted(c[:2] for c in await changesets(maker) if c[0] == "user_edit") == [
        ("user_edit", "expired"),  # same frozen clock: compared unordered
        ("user_edit", "pending"),
    ]
    # The direct apply stands; the (iii) pin is neither applied nor staged (next sync).
    applied = await block_at(maker, stable, 4, "08:00")
    assert (applied.span.upper, applied.gcal_etag) == (at(4, "08:30"), etag)
    assert (await block_at(maker, stable, 2, "08:00")).lineage_id == wed.lineage_id
    assert [c for c in await changesets(maker) if c[0] == "google_edit"] == []
    # The knock-on is inside the user's proposal: the shortened Friday stays as the user set
    # it, and the lost 30 minutes are placed again (same Workout total as before).
    fri_blocks = [
        b["span"] for b in fresh["plan"]["blocks"] if b["lineage_id"] == str(fri.lineage_id)
    ]
    assert fri_blocks == [{"start": z(at(4, "08:00")), "end": z(at(4, "08:30"))}]
    assert workout_minutes(fresh["plan"]) == workout_minutes(pending["plan"])

    # Rejecting it is now an informed decision on the knock-on: the Google edit stays live
    # and pinned, nothing is re-pended, nothing is written back, nothing else is pending.
    await settle(app)
    writes = len(cal.event_calls)
    plan = await live_plan_id(maker)
    await ok(client.post(f"/changesets/{fresh['id']}/reject"), 200)
    await settle(app)
    assert await live_plan_id(maker) == plan
    assert len(cal.event_calls) == writes
    assert [c for c in await changesets(maker) if c[1] == "pending"] == []


async def _pinned_friday(app: Any, client: Any, maker: Any, uid: uuid.UUID) -> Any:
    """Friday's Workout moved in the app to 09:00–10:00 (a confirmed pin), synced."""
    fri = await block_at(maker, uid, 4, "08:00")
    edit = {"action": "edit", "span": {"start": z(at(4, "09:00")), "end": z(at(4, "10:00"))}}
    pinned = await ok(client.patch(f"/blocks/{fri.id}", json=edit))
    await ok(client.post(f"/changesets/{pinned['id']}/confirm"), 200)
    await settle(app)
    await sync(client)
    return await block_at(maker, uid, 4, "09:00")


async def test_restage_drops_the_pin_removal_the_google_edit_replaced(
    app, stable, client, maker, cal: FakeCalendar
):
    """Review round 2: the pending delete of a pinned block stages `pin_removed` X; the user
    then shortens that block in Google, which replaces X directly. The pending changeset is
    restaged WITHOUT that `pin_removed` (the rest kept), so it stays confirmable."""
    moved = await _pinned_friday(app, client, maker, stable)
    pending = await ok(client.patch(f"/blocks/{moved.id}", json={"action": "delete"}))
    assert [m["kind"] for m in pending["staged"]] == ["pin_removed", "pin_added"]

    etag = cal.user_move(CAL1, eid(moved), at(4, "09:00"), at(4, "09:30"))
    fresh = (await sync(client))["pending_changeset"]
    assert fresh["id"] != pending["id"] and fresh["reason"] == "user_edit"
    assert fresh["staged"] == pending["staged"][1:]  # only the forbid is left
    applied = await block_at(maker, stable, 4, "09:00")
    assert (applied.span.upper, applied.gcal_etag) == (at(4, "09:30"), etag)
    assert sorted(c[:2] for c in await changesets(maker) if c[0] == "user_edit")[-2:] == [
        ("user_edit", "expired"),
        ("user_edit", "pending"),
    ]
    resp = await client.post(f"/changesets/{fresh['id']}/confirm")
    assert resp.status_code in (200, 409), resp.text  # confirmable (or stale): never a 404
    assert resp.status_code == 200 or resp.json()["error"]["code"] == "changeset_stale"


async def test_restage_that_empties_the_list_expires_it_and_stages_the_knock_on(
    app, stable, client, maker, cal: FakeCalendar
):
    """The pending changeset only removed the pin the Google edit replaced: nothing is left,
    so it is expired (never left actionable) and the knock-on is the usual google_edit."""
    moved = await _pinned_friday(app, client, maker, stable)
    [(pin_id,)] = await rows(maker, "SELECT id FROM block_pins")
    pending = await ok(client.delete(f"/pins/{pin_id}"))
    assert [m["kind"] for m in pending["staged"]] == ["pin_removed"]

    cal.user_move(CAL1, eid(moved), at(4, "09:00"), at(4, "09:30"))
    cs = (await sync(client))["pending_changeset"]
    assert (cs["reason"], cs["staged"]) == ("google_edit", [])
    status = await rows(maker, "SELECT status::text FROM changesets WHERE id = :i", i=pending["id"])
    assert status == [("expired",)]
    assert (await block_at(maker, stable, 4, "09:00")).span.upper == at(4, "09:30")


async def test_restaged_changeset_is_stamped_later_than_the_one_it_replaces(
    stable, client, maker, cal: FakeCalendar, clock, monkeypatch
):
    """Review round 2: a changeset staged while the sync awaited Google (later than the sync's
    start) is replaced by one stamped at the locked write, strictly later."""
    fri = await block_at(maker, stable, 4, "08:00")
    cal.user_move(CAL1, eid(fri), at(4, "08:00"), at(4, "08:30"))  # case (ii)
    original = gcal_sync.fetch_window
    late = NOW + timedelta(minutes=5)

    async def stage_meanwhile(*args: Any, **kwargs: Any) -> Any:
        clock.current = late
        async with maker() as s, s.begin():
            await propose_changeset(
                s,
                stable,
                extra_staged([exception_added(at(6, "08:00"), at(6, "20:00"))]),
                now=late,
                source="manual",
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(gcal_sync, "fetch_window", stage_meanwhile)
    fresh = (await sync(client))["pending_changeset"]
    assert fresh["reason"] == "user_edit"
    stamps = dict(
        await rows(
            maker, "SELECT status::text, created_at FROM changesets WHERE reason = 'user_edit'"
        )
    )
    assert stamps["pending"] > stamps["expired"] == late


async def test_several_edits_make_one_combined_changeset(stable, client, maker, cal: FakeCalendar):
    wed = await block_at(maker, stable, 2, "08:00")
    fri = await block_at(maker, stable, 4, "08:00")
    cal.user_move(CAL1, eid(fri), at(4, "08:00"), at(4, "08:30"))  # (ii): applied
    cal.user_move(CAL1, eid(wed), at(3, "08:30"), at(3, "09:30"))  # (iii): staged

    cs = (await sync(client))["pending_changeset"]
    assert cs["reason"] == "google_edit"
    assert [m["payload"]["created_from_block_id"] for m in cs["staged"]] == [str(wed.id)]
    assert [c for c in await changesets(maker) if c[0] == "google_edit"] == [
        ("google_edit", "pending", 1)
    ]
    assert (await block_at(maker, stable, 4, "08:00")).span.upper == at(4, "08:30")


async def test_missing_event_lookups_are_capped_per_sync(
    stable, client, maker, cal: FakeCalendar, monkeypatch
):
    from app.gcal import ingest

    monkeypatch.setattr(ingest, "MAX_LOOKUPS", 1)
    wed = await block_at(maker, stable, 2, "08:00")
    thu = await block_at(maker, stable, 3, "08:00")
    cal.user_purge(CAL1, eid(wed))
    cal.user_purge(CAL1, eid(thu))
    await sync(client)
    lineages = {b.lineage_id for b in await live_blocks(maker, stable)}
    assert wed.lineage_id not in lineages and thu.lineage_id in lineages  # nearest first
    assert [r[0] for r in cal.event_reads if r[0]] == [eid(wed)]


async def test_a_failed_lookup_keeps_the_edits_already_read(
    stable, client, maker, cal: FakeCalendar
):
    wed = await block_at(maker, stable, 2, "08:00")
    thu = await block_at(maker, stable, 3, "08:00")
    sat = await block_at(maker, stable, 5, "08:00")
    cal.user_purge(CAL1, eid(wed))
    cal.user_purge(CAL1, eid(thu))
    cal.user_move(CAL1, eid(sat), at(5, "10:00"), at(5, "11:00"))  # listed
    cal.event_fault = lambda m, e: (
        Reply(status=503, json={}) if (m, e) == ("GET", eid(wed)) else None
    )
    await sync(client)  # 200: the failed lookup does not fail the phase
    live = {b.lineage_id: b for b in await live_blocks(maker, stable)}
    assert wed.lineage_id in live  # unjudged this sync
    assert thu.lineage_id not in live  # its lookup succeeded: delete applied
    assert live[sat.lineage_id].span.lower == at(5, "10:00")  # the listed move applied


async def test_listing_follows_every_page(stable, client, maker, cal: FakeCalendar):
    cal.list_page_size = 2
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))
    assert (await sync(client))["pending_changeset"] is None
    assert (await block_at(maker, stable, 3, "10:00")).lineage_id == wed.lineage_id
    assert any("pageToken" in params for e, params in cal.event_reads if e == "")


# ---------------------------------------------------------------- reject undoes the Google move


async def test_rejecting_a_google_edit_restores_our_event(app, stable, client, maker, cal):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "08:30"), at(3, "09:30"))
    cs = (await sync(client))["pending_changeset"]
    assert cs["reason"] == "google_edit"

    await ok(client.post(f"/changesets/{cs['id']}/reject"), 200)
    await settle(app)
    ev = cal.events[(CAL1, eid(wed))]
    assert (ev["start"], ev["end"]) == (
        {"dateTime": z(at(2, "08:00"))},
        {"dateTime": z(at(2, "09:00"))},
    )
    row = await block_at(maker, stable, 2, "08:00")
    assert (row.gcal_status, row.gcal_etag) == ("synced", ev["etag"])

    assert (await sync(client))["pending_changeset"] is None  # our etag: nothing re-proposed
    assert [c[:2] for c in await changesets(maker) if c[0] == "google_edit"] == [
        ("google_edit", "rejected")
    ]


@pytest.mark.parametrize("edit", ["shortened", "deleted"])
async def test_rejecting_an_empty_google_edit_only_declines_the_knock_on(
    app, stable, client, maker, cal: FakeCalendar, edit
):
    """Review round 1 (user decision): the edit already went live and pinned (case ii, or a
    delete); rejecting the empty knock-on proposal keeps it, re-pends nothing and writes
    nothing back to Google."""
    wed = await block_at(maker, stable, 2, "08:00")
    if edit == "shortened":
        cal.user_move(CAL1, eid(wed), at(2, "08:00"), at(2, "08:30"))
    else:
        cal.user_delete(CAL1, eid(wed))
    cs = (await sync(client))["pending_changeset"]
    assert (cs["reason"], cs["staged"]) == ("google_edit", [])
    await settle(app)
    plan, before = await live_plan_id(maker), await spans(maker, stable)
    writes = len(cal.event_calls)
    event = dict(cal.events[(CAL1, eid(wed))])

    await ok(client.post(f"/changesets/{cs['id']}/reject"), 200)
    await settle(app)
    assert (await live_plan_id(maker), await spans(maker, stable)) == (plan, before)
    assert {b.gcal_status for b in await live_blocks(maker, stable)} == {"synced"}
    assert len(cal.event_calls) == writes
    assert cal.events[(CAL1, eid(wed))] == event  # the user's edit stays in Google
    pins = [k for (k,) in await rows(maker, "SELECT kind::text FROM block_pins")]
    assert pins == (["pin"] if edit == "shortened" else ["forbid"])
    assert (await sync(client))["pending_changeset"] is None  # nothing re-proposed


async def test_rejecting_other_changesets_repends_nothing(stable, client, maker):
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    await ok(client.post(f"/changesets/{staged['id']}/reject"), 200)
    assert {b.gcal_status for b in await live_blocks(maker, stable)} == {"synced"}


# ---------------------------------------------------------------- deleted app calendar


async def test_calendar_404_on_the_listing_repends_and_recreates(
    app, stable, client, maker, cal: FakeCalendar
):
    """Deferred from part (a): every block is synced, so the drain writes nothing; the
    listing's 404 is what notices the calendar is gone."""
    cal.gone_calendars.add(CAL1)
    body = await sync(client)
    assert body["pending_changeset"] is None
    assert await rows(maker, "SELECT app_calendar_id FROM google_connections") == [(CAL2,)]
    await settle(app)
    future = [b for b in await live_blocks(maker, stable) if b.span.upper > NOW]
    assert set(cal.calendar_events(CAL2)) == {eid(b) for b in future}
    assert (await sync(client))["outbox"]["pending"] == 0


# ---------------------------------------------------------------- drift


async def test_drift_proposes_a_resolve_with_nothing_staged(stable, client, maker, cal):
    cal.busy = [(at(3, "07:30"), at(3, "09:00"))]  # a meeting lands on Thursday's Workout
    cs = (await sync(client))["pending_changeset"]
    assert (cs["reason"], cs["staged"], cs["status"]) == ("drift", [], "pending")
    thu = await block_at(maker, stable, 3, "08:00")
    entry = next(e for e in cs["diff"]["entries"] if e["lineage_id"] == str(thu.lineage_id))
    assert entry["change"] in ("moved", "removed")
    # The same drift on the next sync returns the pending one; no second proposal.
    assert (await sync(client))["pending_changeset"]["id"] == cs["id"]
    assert [c for c in await changesets(maker) if c[0] == "drift" and c[1] == "pending"] == [
        ("drift", "pending", 0)
    ]


async def test_drift_with_a_pending_changeset_creates_none(stable, client, maker, cal):
    pending = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    cal.busy = [(at(3, "07:30"), at(3, "09:00"))]
    assert (await sync(client))["pending_changeset"]["id"] == pending["id"]
    assert [c[0] for c in await changesets(maker) if c[1] == "pending"] == ["user_edit"]


async def test_no_drift_when_busy_misses_every_block(stable, client, maker, cal):
    cal.busy = [(at(3, "12:00"), at(3, "13:00"))]
    assert (await sync(client))["pending_changeset"] is None
    assert len(cal.freebusy_bodies) >= 1


async def test_busy_failure_is_503_and_applies_no_edit(stable, client, maker, cal):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))
    cal.freebusy = Reply(status=503, json={})
    plan = await live_plan_id(maker)
    body = await sync(client, 503)
    assert body["error"]["code"] == "gcal_unavailable"
    assert await live_plan_id(maker) == plan
    assert await rows(maker, "SELECT count(*) FROM block_pins") == [(0,)]


async def test_grant_revoked_after_the_listing_is_409(stable, client, maker, cal, google):
    """invalid_grant on the busy read's token mint (the listing went through)."""
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))

    def revoke_after_list(method: str, event_id: str) -> None:
        if method == "GET":
            google.refresh = Reply(status=400, json={"error": "invalid_grant"})

    cal.event_fault = revoke_after_list  # type: ignore[assignment]
    plan = await live_plan_id(maker)
    body = await sync(client, 409)
    assert body["error"]["code"] == "gcal_reconnect_required"
    assert await live_plan_id(maker) == plan


# ---------------------------------------------------------------- races (list → lock)


@pytest.mark.parametrize("change", ["etag", "superseded"])
async def test_state_changed_between_list_and_lock_skips_the_edit(
    stable, client, maker, cal, monkeypatch, change
):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))
    original = gcal_sync.fetch_window

    async def racing(*args: Any, **kwargs: Any) -> Any:
        # Runs after the listing, before the lock (the busy read): change the state.
        if change == "etag":
            async with maker() as s, s.begin():
                await s.execute(
                    text("UPDATE blocks SET gcal_etag = '\"raced\"' WHERE id = :b"), {"b": wed.id}
                )
        else:
            async with maker() as s, s.begin():
                cs = await propose_changeset(
                    s,
                    stable,
                    extra_staged([exception_added(at(6, "08:00"), at(6, "20:00"))]),
                    now=NOW,
                )
            await confirm(maker, stable, cs, now=NOW)
        return await original(*args, **kwargs)

    monkeypatch.setattr(gcal_sync, "fetch_window", racing)
    assert (await sync(client))["pending_changeset"] is None
    assert await rows(maker, "SELECT count(*) FROM block_pins") == [(0,)]
    assert [c for c in await changesets(maker) if c[0] == "google_edit"] == []


async def test_a_slow_drain_skips_the_ingest_but_still_answers(
    app, stable, client, maker, cal: FakeCalendar, monkeypatch
):
    from app.api import gcal as gcal_routes

    monkeypatch.setattr(gcal_routes, "SYNC_BUDGET_S", 0.2)
    thu = await block_at(maker, stable, 3, "08:00")
    async with maker() as s, s.begin():
        await s.execute(
            text("UPDATE blocks SET gcal_status = 'pending' WHERE id = :b"), {"b": thu.id}
        )
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(3, "10:00"), at(3, "11:00"))
    reads = len(cal.event_reads)
    gate = cal.event_gate = asyncio.Event()
    body = await sync(client)  # the drain is held on Google: no listing this time
    assert body["pending_changeset"] is None and len(cal.event_reads) == reads
    gate.set()
    await settle(app)
    await sync(client)  # the next sync ingests
    assert (await block_at(maker, stable, 3, "10:00")).lineage_id == wed.lineage_id


async def test_never_connected_skips_ingest_and_drift(maker, who, clock, client, cal):
    cs = await propose(maker, who.user_id, [prefs(), workout(hours=6.0), deadline_project()])
    await confirm(maker, who.user_id, cs)
    clock.current = NOW
    cal.busy = [(NOW, NOW + timedelta(days=5))]
    assert (await sync(client)) == {
        "pending_changeset": None,
        "outbox": {"pending": 0, "synced": 0},
    }
    assert cal.requests == []

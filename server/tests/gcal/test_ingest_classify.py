"""T4.3b detection rules (`app.gcal.ingest.classify`, pure) and the events read client
(`list_events` / `get_event`) against recorded responses. No database, no live Google."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.db.outbox import SyncedRow
from app.gcal.calendar import (
    CalendarNotFoundError,
    EventSnapshot,
    HttpGoogleCalendarClient,
    parse_event,
)
from app.gcal.client import GoogleUnavailableError
from app.gcal.ingest import classify
from app.scheduler import Interval
from tests.gcal.calendar_fakes import CALENDAR_URL, FakeCalendar
from tests.gcal.fakes import ACCESS_TOKEN, Reply, settings

NOW = datetime(2026, 11, 17, 18, 30, tzinfo=UTC)
WEEK = Interval(
    start=datetime(2026, 11, 16, 8, tzinfo=UTC), end=datetime(2026, 11, 23, 8, tzinfo=UTC)
)
H = timedelta(hours=1)
WED = datetime(2026, 11, 18, 16, tzinfo=UTC)
LINEAGE = uuid.UUID("1a2b3c4d-5e6f-4a7b-8c9d-000000000004")
CAL = "cal1@group.calendar.google.com"


def row(start: datetime = WED, etag: str | None = '"1"', **over: object) -> SyncedRow:
    values: dict[str, object] = {
        "block_id": uuid.uuid4(),
        "lineage_id": LINEAGE,
        "span": Interval(start=start, end=start + H),
        "plan_horizon": WEEK,
        "event_id": "ev1",
        "etag": etag,
    }
    return SyncedRow(**(values | over))  # type: ignore[arg-type]


def ev(
    start: datetime | None = WED,
    end: datetime | None = WED + H,
    etag: str = '"2"',
    status: str = "confirmed",
    lineage: str | None = str(LINEAGE),
) -> EventSnapshot:
    return EventSnapshot(
        id="ev1", etag=etag, status=status, start=start, end=end, lineage_id=lineage
    )


def test_same_etag_is_our_own_write() -> None:
    assert classify([row()], {"ev1": ev(start=WED + H, end=WED + 2 * H, etag='"1"')}, NOW) == []


def test_moved_start_or_end_is_a_move() -> None:
    [edit] = classify([row()], {"ev1": ev(start=WED + 2 * H, end=WED + 3 * H)}, NOW)
    assert (edit.kind, edit.new_span, edit.new_etag) == (
        "move",
        Interval(start=WED + 2 * H, end=WED + 3 * H),
        '"2"',
    )
    [resized] = classify([row()], {"ev1": ev(end=WED + H / 2)}, NOW)
    assert resized.kind == "move" and resized.new_span == Interval(start=WED, end=WED + H / 2)


@pytest.mark.parametrize("event", [None, ev(status="cancelled", start=None, end=None)])
def test_cancelled_or_gone_is_a_delete(event: EventSnapshot | None) -> None:
    [edit] = classify([row()], {"ev1": event}, NOW)
    assert (edit.kind, edit.new_span, edit.expected_etag) == ("delete", None, '"1"')


@pytest.mark.parametrize(
    ("event", "why"),
    [
        (ev(), "title/description only: same times"),
        (ev(start=None, end=None), "converted to an all-day event"),
        (ev(start=WED + timedelta(seconds=30)), "off the minute grid"),
        (ev(start=NOW - H, end=NOW), "moved into the past"),
        (ev(start=WEEK.end, end=WEEK.end + H), "moved out of the plan's week"),
        (ev(start=WED + H, end=WED), "end before start"),
        (ev(start=WED + H, end=WED + 2 * H, lineage="someone-else"), "another lineage's event"),
    ],
)
def test_ignored_edits(event: EventSnapshot, why: str) -> None:
    assert classify([row()], {"ev1": event}, NOW) == [], why


def test_started_rows_and_unjudged_rows_are_skipped() -> None:
    started = row(start=NOW - H / 2)
    assert classify([started], {"ev1": None}, NOW) == []
    assert classify([row()], {}, NOW) == []  # not looked up: not judged


def test_edits_come_out_in_block_start_order() -> None:
    late, early = row(start=WED + 5 * H, event_id="b"), row(start=WED, event_id="a")
    events = {"a": None, "b": None}
    assert [e.event_id for e in classify([late, early], events, NOW)] == ["a", "b"]


# ---------------------------------------------------------------- client


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def client(cal: FakeCalendar) -> HttpGoogleCalendarClient:
    return HttpGoogleCalendarClient(settings(calendar_api_url=CALENDAR_URL), cal.transport())


def stored(cal: FakeCalendar, n: int) -> None:
    for i in range(n):
        start = WED + i * H
        cal.events[(CAL, f"ev{i}")] = {
            "id": f"ev{i}",
            "etag": f'"{i}"',
            "status": "cancelled" if i == 0 else "confirmed",
            "summary": "Secret project",
            "description": "Scheduled by ReclaimMyTime",
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
            "end": {"dateTime": (start + H).isoformat().replace("+00:00", "Z")},
            "extendedProperties": {"private": {"lineage_id": str(LINEAGE)}},
        }


async def test_list_events_reads_every_page_and_only_the_fields_we_need(client, cal) -> None:
    stored(cal, 5)
    cal.list_page_size = 2
    got = await client.list_events(ACCESS_TOKEN, CAL, time_min=WEEK.start, time_max=WEEK.end)
    assert [e.id for e in got] == [f"ev{i}" for i in range(5)]  # cancelled included
    assert got[0].status == "cancelled" and got[1].start == WED + H
    assert got[1].lineage_id == str(LINEAGE)
    reads = cal.event_reads
    assert len(reads) == 3 and reads[1][1]["pageToken"] == "2"
    assert reads[0][1]["fields"] == (
        "items(id,etag,status,start,end,extendedProperties/private),nextPageToken"
    )
    assert cal.requests[0][3] == f"Bearer {ACCESS_TOKEN}"


async def test_list_events_404_is_calendar_gone_and_5xx_unavailable(client, cal) -> None:
    cal.gone_calendars.add(CAL)
    with pytest.raises(CalendarNotFoundError):
        await client.list_events(ACCESS_TOKEN, CAL, time_min=WEEK.start, time_max=WEEK.end)
    cal.gone_calendars.clear()
    cal.event_fault = lambda m, e: Reply(status=500, json={})
    with pytest.raises(GoogleUnavailableError):
        await client.list_events(ACCESS_TOKEN, CAL, time_min=WEEK.start, time_max=WEEK.end)


async def test_get_event_found_or_none(client, cal) -> None:
    stored(cal, 2)
    found = await client.get_event(ACCESS_TOKEN, CAL, "ev1")
    assert found is not None and (found.etag, found.start) == ('"1"', WED + H)
    assert await client.get_event(ACCESS_TOKEN, CAL, "nope") is None


async def test_list_events_rejects_a_malformed_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": "not a list"})

    client = HttpGoogleCalendarClient(
        settings(calendar_api_url=CALENDAR_URL), httpx.MockTransport(handler)
    )
    with pytest.raises(GoogleUnavailableError):
        await client.list_events(ACCESS_TOKEN, CAL, time_min=WEEK.start, time_max=WEEK.end)


def test_parse_event_tolerates_partial_items() -> None:
    assert parse_event({"etag": "x"}) is None
    assert parse_event("nope") is None
    snap = parse_event({"id": "a", "status": "cancelled"})
    assert snap is not None and (snap.etag, snap.start, snap.lineage_id) == ("", None, None)
    allday = parse_event(
        {"id": "a", "start": {"date": "2026-11-18"}, "end": {"date": "2026-11-19"}}
    )
    assert allday is not None and allday.start is None

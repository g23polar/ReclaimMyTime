"""T4.3a Calendar REST event calls (insert/update/delete) against recorded responses, and the
deterministic event id. No database, no live Google."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date

import httpx
import pytest

from app.db.outbox import event_id_for
from app.gcal.calendar import (
    CalendarNotFoundError,
    EventExistsError,
    EventNotFoundError,
    EventRejectedError,
    HttpGoogleCalendarClient,
)
from app.gcal.client import GoogleUnavailableError
from tests.gcal.calendar_fakes import CALENDAR_URL, FakeCalendar
from tests.gcal.fakes import ACCESS_TOKEN, Reply, settings

CAL = "cal1@group.calendar.google.com"
EID = "0123456789abcdef0123456789abcdef20261116"
BODY = {"summary": "Secret project", "start": {"dateTime": "2026-11-18T15:00:00Z"}}


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def client(cal: FakeCalendar) -> HttpGoogleCalendarClient:
    return HttpGoogleCalendarClient(settings(calendar_api_url=CALENDAR_URL), cal.transport())


def test_event_id_is_base32hex_and_unique_per_lineage_week() -> None:
    lineage = uuid.UUID("1a2b3c4d-5e6f-4a7b-8c9d-000000000004")
    this_week = event_id_for(lineage, date(2026, 11, 16))
    assert re.fullmatch(r"[0-9a-v]{5,1024}", this_week)
    assert this_week == "1a2b3c4d5e6f4a7b8c9d00000000000420261116"
    # pack mints lineages per week, so the same lineage id next week is another event.
    assert event_id_for(lineage, date(2026, 11, 23)) != this_week


async def test_insert_returns_the_etag_and_sends_the_bearer(client, cal) -> None:
    etag = await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    assert etag == cal.events[(CAL, EID)]["etag"]
    method, url, body, auth = cal.requests[-1]
    assert (method, auth) == ("POST", f"Bearer {ACCESS_TOKEN}")
    assert httpx.URL(url).path.endswith("/calendars/cal1@group.calendar.google.com/events")
    assert body == {"id": EID, **BODY}


async def test_insert_status_mapping(client, cal) -> None:
    await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    with pytest.raises(EventExistsError):
        await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    cal.gone_calendars.add(CAL)
    with pytest.raises(CalendarNotFoundError):
        await client.insert_event(ACCESS_TOKEN, CAL, {"id": "other12345", **BODY})


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, EventRejectedError),
        (401, GoogleUnavailableError),
        (403, GoogleUnavailableError),
        (429, GoogleUnavailableError),
        (500, GoogleUnavailableError),
        (503, GoogleUnavailableError),
    ],
)
async def test_failures_are_rejected_or_transient(client, cal, status, error) -> None:
    cal.event_fault = lambda m, e: Reply(status=status, json={"error": {"code": status}})
    for call in (
        client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY}),
        client.update_event(ACCESS_TOKEN, CAL, EID, BODY),
        client.delete_event(ACCESS_TOKEN, CAL, EID),
    ):
        with pytest.raises(error):
            await call


async def test_network_error_is_transient(client, cal) -> None:
    cal.event_fault = lambda m, e: Reply(raise_exc=httpx.ConnectTimeout("t"))
    with pytest.raises(GoogleUnavailableError):
        await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})


async def test_an_answer_without_an_etag_is_transient(client, cal) -> None:
    cal.event_fault = lambda m, e: Reply(status=200, json={"id": EID})
    with pytest.raises(GoogleUnavailableError):
        await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})


async def test_update_replaces_and_revives_and_404_or_410_is_not_found(client, cal) -> None:
    with pytest.raises(EventNotFoundError):
        await client.update_event(ACCESS_TOKEN, CAL, EID, BODY)
    await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    await client.delete_event(ACCESS_TOKEN, CAL, EID)
    assert cal.events[(CAL, EID)]["status"] == "cancelled"
    etag = await client.update_event(ACCESS_TOKEN, CAL, EID, {**BODY, "status": "confirmed"})
    assert cal.events[(CAL, EID)]["status"] == "confirmed"
    assert etag == cal.events[(CAL, EID)]["etag"]
    cal.event_fault = lambda m, e: Reply(status=410, json={})
    with pytest.raises(EventNotFoundError):
        await client.update_event(ACCESS_TOKEN, CAL, EID, BODY)


async def test_delete_of_a_missing_or_gone_event_is_success(client, cal) -> None:
    await client.delete_event(ACCESS_TOKEN, CAL, EID)  # 404
    await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    await client.delete_event(ACCESS_TOKEN, CAL, EID)  # 204
    await client.delete_event(ACCESS_TOKEN, CAL, EID)  # 410
    assert [m for m, _ in cal.event_calls] == ["DELETE", "POST", "DELETE", "DELETE"]


async def test_event_bodies_are_never_logged(client, cal, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cal.event_fault = lambda m, e: Reply(status=400, json={"error": {"message": "Secret project"}})
    with pytest.raises(EventRejectedError):
        await client.insert_event(ACCESS_TOKEN, CAL, {"id": EID, **BODY})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Secret project" not in text and CAL not in text and ACCESS_TOKEN not in text
    assert "400" in text

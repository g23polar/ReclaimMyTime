"""T4.2 Calendar REST client + busy normalization against recorded responses (MockTransport).
No database, no live Google."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.gcal.busy import normalize_busy
from app.gcal.calendar import (
    APP_CALENDAR_SUMMARY,
    BusySpan,
    CalendarNotFoundError,
    HttpGoogleCalendarClient,
)
from app.gcal.client import GoogleUnavailableError
from app.gcal.settings import GcalSettings
from app.scheduler import Interval
from tests.gcal.calendar_fakes import (
    CALENDAR_URL,
    CALENDARS_URL,
    FREEBUSY_URL,
    FakeCalendar,
    busy_body,
)
from tests.gcal.fakes import ACCESS_TOKEN, Reply, settings

H0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)  # Mon 00:00 LA
HORIZON = Interval(start=H0, end=H0 + timedelta(days=7))


def t(hours: float) -> datetime:
    return H0 + timedelta(hours=hours)


def iv(a: float, b: float) -> Interval:
    return Interval(start=t(a), end=t(b))


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def client(cal: FakeCalendar) -> HttpGoogleCalendarClient:
    return HttpGoogleCalendarClient(settings(calendar_api_url=CALENDAR_URL), cal.transport())


# ---------------------------------------------------------------- freeBusy


async def test_free_busy_queries_primary_only_over_the_window(cal, client):
    cal.busy = [(t(10), t(11))]
    spans = await client.free_busy(ACCESS_TOKEN, time_min=HORIZON.start, time_max=HORIZON.end)
    assert spans == [BusySpan(t(10), t(11))]
    [(method, url, body, auth)] = cal.requests
    assert (method, url, auth) == ("POST", FREEBUSY_URL, f"Bearer {ACCESS_TOKEN}")
    # Decision T4.2 #1: primary only — the app calendar (never primary) is excluded.
    assert body == {
        "timeMin": "2026-11-16T08:00:00Z",
        "timeMax": "2026-11-23T08:00:00Z",
        "items": [{"id": "primary"}],
    }


async def test_free_busy_parses_offsets_to_utc_and_drops_empty_spans(cal, client):
    cal.freebusy = Reply(
        json={
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-11-17T09:00:00-08:00", "end": "2026-11-17T10:00:00-08:00"},
                        {"start": "2026-11-18T17:00:00Z", "end": "2026-11-18T17:00:00Z"},
                    ]
                }
            }
        }
    )
    spans = await client.free_busy(ACCESS_TOKEN, time_min=HORIZON.start, time_max=HORIZON.end)
    assert spans == [
        BusySpan(datetime(2026, 11, 17, 17, tzinfo=UTC), datetime(2026, 11, 17, 18, tzinfo=UTC))
    ]


async def test_free_busy_empty(cal, client):
    assert await client.free_busy(ACCESS_TOKEN, time_min=t(0), time_max=t(168)) == []


@pytest.mark.parametrize(
    "reply",
    [
        Reply(json=busy_body(errors=[{"domain": "global", "reason": "backendError"}])),
        Reply(json=busy_body(errors=[{"domain": "global", "reason": "notFound"}])),
        Reply(status=500, json={"error": {"code": 500}}),
        Reply(status=503, json={}),
        Reply(status=401, json={}),
        Reply(status=403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}),
        Reply(raise_exc=httpx.ReadTimeout("slow")),
        Reply(raise_exc=httpx.ConnectError("down")),
        Reply(json={"calendars": {}}),
        Reply(json={"calendars": {"primary": {"busy": [{"start": "nope", "end": "x"}]}}}),
        Reply(json={"calendars": {"primary": {"busy": [{"start": "2026-11-17T09:00:00"}]}}}),
        Reply(json=["not", "an", "object"]),
        # Review round 1: malformed answers are never read as "free".
        Reply(json={"kind": "calendar#freeBusy"}),
        Reply(json={"calendars": {"primary": {}}}),
        Reply(json={"calendars": {"primary": {"busy": None}}}),
        Reply(json={"calendars": {"primary": {"busy": {"start": "x"}}}}),
        Reply(json={"calendars": {"primary": "busy"}}),
        Reply(json={"calendars": {"primary": {"busy": [{"start": 1700000000, "end": 1}]}}}),
        Reply(
            json={
                "calendars": {
                    "primary": {
                        "busy": [{"start": "2026-11-17T18:00:00Z", "end": "2026-11-17T17:00:00Z"}]
                    }
                }
            }
        ),
    ],
    ids=[
        "errors-backend",
        "errors-notfound",
        "500",
        "503",
        "401",
        "403-rate",
        "timeout",
        "connect",
        "no-primary",
        "bad-instant",
        "naive-no-end",
        "not-object",
        "no-calendars",
        "no-busy",
        "busy-null",
        "busy-not-list",
        "primary-not-object",
        "instant-not-string",
        "end-before-start",
    ],
)
async def test_free_busy_failures_are_unavailable_and_log_no_secrets(cal, client, reply, caplog):
    cal.freebusy = reply
    caplog.set_level(logging.DEBUG)
    with pytest.raises(GoogleUnavailableError):
        await client.free_busy(ACCESS_TOKEN, time_min=t(0), time_max=t(168))
    assert ACCESS_TOKEN not in caplog.text
    assert "rateLimitExceeded" not in caplog.text  # no response bodies


# ---------------------------------------------------------------- normalize_busy


def test_normalize_merges_clips_and_sorts():
    spans = [
        BusySpan(t(30), t(32)),
        BusySpan(t(10), t(12)),
        BusySpan(t(11), t(13)),  # overlaps → merged
        BusySpan(t(13), t(14)),  # touches → merged
        BusySpan(t(-5), t(1)),  # clipped at the horizon start
        BusySpan(t(167), t(170)),  # clipped at the horizon end
        BusySpan(t(-9), t(-8)),  # wholly before → dropped
        BusySpan(t(200), t(201)),  # wholly after → dropped
    ]
    assert normalize_busy(spans, HORIZON) == (
        iv(0, 1),
        iv(10, 14),
        iv(30, 32),
        iv(167, 168),
    )


def test_normalize_widens_to_whole_minutes():
    start = t(10) + timedelta(seconds=30)
    end = t(11) + timedelta(seconds=1)
    [got] = normalize_busy([BusySpan(start, end)], HORIZON)
    assert (got.start, got.end) == (t(10), t(11) + timedelta(minutes=1))


def test_normalize_empty():
    assert normalize_busy([], HORIZON) == ()


# ---------------------------------------------------------------- calendars


async def test_create_calendar_posts_summary_and_returns_id(cal, client):
    got = await client.create_calendar(ACCESS_TOKEN, APP_CALENDAR_SUMMARY)
    assert got == "cal1@group.calendar.google.com"
    [(method, url, body, auth)] = cal.requests
    assert (method, url, body) == ("POST", CALENDARS_URL, {"summary": "ReclaimMyTime"})
    assert auth == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=500, json={}),
        Reply(json={"kind": "calendar#calendar"}),
        Reply(raise_exc=httpx.ReadTimeout("x")),
    ],
    ids=["500", "no-id", "timeout"],
)
async def test_create_calendar_failures_are_unavailable(cal, client, reply):
    cal.create = reply
    with pytest.raises(GoogleUnavailableError):
        await client.create_calendar(ACCESS_TOKEN, APP_CALENDAR_SUMMARY)


async def test_delete_calendar_quotes_the_id_and_maps_404(cal, client):
    await client.delete_calendar(ACCESS_TOKEN, "abc@group.calendar.google.com")
    assert cal.requests[0][1] == f"{CALENDARS_URL}/abc%40group.calendar.google.com"
    cal.delete = Reply(status=404, json={})
    with pytest.raises(CalendarNotFoundError):
        await client.delete_calendar(ACCESS_TOKEN, "gone")
    cal.delete = Reply(status=500, json={})
    with pytest.raises(GoogleUnavailableError):
        await client.delete_calendar(ACCESS_TOKEN, "x")


# ---------------------------------------------------------------- settings


def test_calendar_api_url_default_and_env_override():
    assert GcalSettings().calendar_api_url == "https://www.googleapis.com/calendar/v3"
    env = {
        "GOOGLE_CLIENT_ID": "id",
        "GOOGLE_CLIENT_SECRET": "secret",
        "GOOGLE_REDIRECT_URI": "https://api.example/gcal/callback",
        "GCAL_STATE_KEY": settings().state_key,
        "GOOGLE_CALENDAR_API_URL": "https://calendar.mock/v3/",
    }
    assert GcalSettings.from_env(env).calendar_api_url == "https://calendar.mock/v3"

"""Google Calendar REST client (T4.2): freeBusy on the primary calendar, and create/delete of
the app-owned secondary calendar (both allowed under our two narrow scopes).

Same discipline as app.gcal.client: the access token is passed in per call, held in memory
only, and never logged; neither are response bodies. Log lines carry the HTTP status or the
exception type only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.gcal.client import GoogleUnavailableError
from app.gcal.settings import GcalSettings

logger = logging.getLogger(__name__)

APP_CALENDAR_SUMMARY = "ReclaimMyTime"

# v1 reads the PRIMARY calendar only (decision T4.2 #1). Under calendar.freebusy +
# calendar.app.created we cannot list the user's calendars, so other secondary calendars are
# not consulted (known limitation). The app calendar is never primary, so our own blocks are
# excluded from busy time by construction (ARCH §3: "app calendar excluded").
PRIMARY = "primary"


class CalendarNotFoundError(Exception):
    """Google answered 404 for a calendar id (e.g. the user deleted the app calendar)."""


@dataclass(frozen=True)
class BusySpan:
    """One raw freeBusy interval, absolute UTC, exactly as Google reported it."""

    start: datetime
    end: datetime


class GoogleCalendarClient(Protocol):
    async def free_busy(
        self, access_token: str, *, time_min: datetime, time_max: datetime
    ) -> list[BusySpan]:
        """Busy spans of the primary calendar in [time_min, time_max). Unordered, unclipped.
        GoogleUnavailableError on any failure, including a per-calendar `errors` entry."""
        ...

    async def create_calendar(self, access_token: str, summary: str) -> str:
        """Create a secondary calendar; returns its id. GoogleUnavailableError."""
        ...

    async def delete_calendar(self, access_token: str, calendar_id: str) -> None:
        """Delete an app-created calendar. CalendarNotFoundError | GoogleUnavailableError."""
        ...


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("not a string")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC)


def parse_free_busy(body: Any) -> list[BusySpan]:
    """`calendars.primary.busy[]` → spans. A per-calendar `errors` entry, a missing
    `calendars` / `primary` / `busy`, a non-list `busy`, an unparseable or naive timestamp, or
    a span ending before it starts is an unexpected answer (→ GoogleUnavailableError): we fail
    closed rather than solve as if the user were free. end == start is an empty span, dropped."""
    try:
        entry = body["calendars"][PRIMARY]
        if not isinstance(entry, dict):
            raise TypeError("primary entry is not an object")
        if entry.get("errors"):
            # Count only: the entries are Google's response body (never logged).
            errors = entry["errors"]
            count = len(errors) if isinstance(errors, list) else 1
            logger.warning("google freeBusy reported %d calendar error(s)", count)
            raise GoogleUnavailableError()
        busy = entry["busy"]  # required: a missing list is not "free"
        if not isinstance(busy, list):
            raise TypeError("busy is not a list")
        spans = []
        for item in busy:
            start, end = _parse_instant(item["start"]), _parse_instant(item["end"])
            if end < start:
                raise ValueError("busy span ends before it starts")
            if start < end:  # an empty span (end == start) blocks nothing
                spans.append(BusySpan(start, end))
        return spans
    except (KeyError, TypeError, ValueError):
        logger.warning("google freeBusy returned an unexpected body")
        raise GoogleUnavailableError() from None


class HttpGoogleCalendarClient:
    """https://developers.google.com/calendar/api/v3/reference (freebusy.query,
    calendars.insert, calendars.delete)."""

    def __init__(
        self, settings: GcalSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = settings.calendar_api_url.rstrip("/")
        self._timeout_s = settings.http_timeout_s
        self._transport = transport

    async def _request(
        self, method: str, path: str, access_token: str, json: Any | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                return await client.request(
                    method, f"{self._base}{path}", headers=headers, json=json
                )
        except httpx.HTTPError as exc:
            logger.warning("google calendar %s failed: %s", method, type(exc).__name__)
            raise GoogleUnavailableError() from None

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        if response.status_code != 200:
            # 401/403 with a token minted moments ago is a Google-side hiccup or a quota
            # limit, not a revoked grant (refresh would have said invalid_grant): transient.
            logger.warning("google %s returned %s", what, response.status_code)
            raise GoogleUnavailableError()
        try:
            return response.json()
        except ValueError:
            logger.warning("google %s returned an unparseable body", what)
            raise GoogleUnavailableError() from None

    async def free_busy(
        self, access_token: str, *, time_min: datetime, time_max: datetime
    ) -> list[BusySpan]:
        body = {
            "timeMin": _rfc3339(time_min),
            "timeMax": _rfc3339(time_max),
            "items": [{"id": PRIMARY}],  # primary only; see PRIMARY
        }
        response = await self._request("POST", "/freeBusy", access_token, json=body)
        return parse_free_busy(self._json(response, "freeBusy"))

    async def create_calendar(self, access_token: str, summary: str) -> str:
        response = await self._request(
            "POST", "/calendars", access_token, json={"summary": summary}
        )
        body = self._json(response, "calendars.insert")
        calendar_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(calendar_id, str) or not calendar_id:
            logger.warning("google calendars.insert answer lacks an id")
            raise GoogleUnavailableError()
        return calendar_id

    async def delete_calendar(self, access_token: str, calendar_id: str) -> None:
        path = f"/calendars/{quote(calendar_id, safe='')}"
        response = await self._request("DELETE", path, access_token)
        if response.status_code in (200, 204):
            return
        if response.status_code in (404, 410):
            raise CalendarNotFoundError()
        logger.warning("google calendars.delete returned %s", response.status_code)
        raise GoogleUnavailableError()

"""Recorded-HTTP Google Calendar for T4.2 tests (freeBusy, calendars.insert/delete) plus helpers
that seed a Google connection row and install a gcal runtime. Nothing here talks to Google.

T4.3a adds an in-memory event store for the write-back (events.insert/update/delete), with
Google's semantics as far as the worker relies on them: a client-supplied id that exists
(even cancelled) → 409 on insert; update of a cancelled event revives it; delete of a cancelled
event → 410; any event call on a calendar in `gone_calendars` → 404.

T4.3b adds events.list (showDeleted, time window, pagination via `list_page_size`) and
events.get, answering only the `fields` the ingest asks for, plus `user_*` helpers that
simulate edits made inside Google (each bumps the etag, as Google does)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.clock import Clock
from app.auth.crypto import TokenCipher
from app.db.models import GoogleConnection
from app.gcal.calendar import HttpGoogleCalendarClient
from app.gcal.client import HttpGoogleOAuthClient
from app.gcal.runtime import GcalRuntime, build_gcal_runtime
from app.gcal.settings import CALENDAR_SCOPES
from tests.gcal.fakes import EMAIL, REFRESH_TOKEN, FakeGoogle, Reply, settings

CALENDAR_URL = "https://calendar.test/calendar/v3"
FREEBUSY_URL = f"{CALENDAR_URL}/freeBusy"
CALENDARS_URL = f"{CALENDAR_URL}/calendars"
CALENDARS_PATH = httpx.URL(CALENDARS_URL).path


def z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def busy_body(*spans: tuple[datetime, datetime], errors: list[Any] | None = None) -> Any:
    """A Google-shaped freeBusy answer for the primary calendar."""
    entry: dict[str, Any] = {"busy": [{"start": z(s), "end": z(e)} for s, e in spans]}
    if errors is not None:
        entry["errors"] = errors
    return {
        "kind": "calendar#freeBusy",
        "timeMin": "2026-11-16T08:00:00.000Z",
        "timeMax": "2026-11-23T08:00:00.000Z",
        "calendars": {"primary": entry},
    }


@dataclass
class FakeCalendar:
    """`busy` drives freeBusy unless `freebusy` overrides the whole reply. Calendars created get
    ids from `ids`; `on_create` (async) runs before the create answer is returned."""

    busy: list[tuple[datetime, datetime]] = field(default_factory=list)
    freebusy: Reply | None = None
    create: Reply | None = None
    delete: Reply = field(default_factory=lambda: Reply(status=204))
    ids: list[str] = field(
        default_factory=lambda: [f"cal{i}@group.calendar.google.com" for i in range(1, 10)]
    )
    on_create: Callable[[], Awaitable[None]] | None = None
    requests: list[tuple[str, str, Any, str | None]] = field(default_factory=list)
    # ---- events (T4.3a)
    events: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    gone_calendars: set[str] = field(default_factory=set)
    # (method, event_id) → a reply that replaces the normal answer, or None for normal.
    event_fault: Callable[[str, str], Reply | None] | None = None
    # Every event call waits on this when set (concurrency tests hold the drain mid-flight).
    event_gate: asyncio.Event | None = None
    event_started: asyncio.Event = field(default_factory=asyncio.Event)
    list_page_size: int = 250
    _etags: int = 0

    @property
    def freebusy_bodies(self) -> list[Any]:
        return [body for m, url, body, _ in self.requests if url == FREEBUSY_URL]

    @property
    def created(self) -> int:
        return sum(1 for m, url, _, _ in self.requests if m == "POST" and url == CALENDARS_URL)

    @property
    def deleted(self) -> list[str]:
        """Calendars deleted (events.delete calls are in `event_calls`)."""
        prefix = CALENDARS_URL + "/"
        return [
            httpx.URL(url).path.rsplit("/", 1)[1]
            for m, url, _, _ in self.requests
            if m == "DELETE" and url.startswith(prefix) and "/events" not in url
        ]

    @property
    def event_calls(self) -> list[tuple[str, str]]:
        """(method, event id) of every events.* call, in order (insert: the body's id)."""
        out = []
        for m, url, body, _ in self.requests:
            path = httpx.URL(url).path
            if "/events" not in path:
                continue
            if m == "GET":
                continue  # reads (T4.3b ingest) are in `event_reads`
            tail = path.split("/events", 1)[1].lstrip("/")
            out.append((m, tail or (body or {}).get("id", "")))
        return out

    @property
    def event_reads(self) -> list[tuple[str, dict[str, str]]]:
        """(event id or "" for events.list, query params) of every events.list/get call."""
        out = []
        for m, url, _, _ in self.requests:
            u = httpx.URL(url)
            if m != "GET" or "/events" not in u.path:
                continue
            out.append((u.path.split("/events", 1)[1].lstrip("/"), dict(u.params)))
        return out

    # ---- simulated edits made by the user inside Google (T4.3b)

    def _bump(self, event: dict[str, Any]) -> None:
        self._etags += 1
        event["etag"] = f'"{self._etags:06d}"'

    def user_move(self, calendar_id: str, event_id: str, start: datetime, end: datetime) -> str:
        ev = self.events[(calendar_id, event_id)]
        ev["start"], ev["end"] = {"dateTime": z(start)}, {"dateTime": z(end)}
        self._bump(ev)
        return str(ev["etag"])

    def user_retitle(self, calendar_id: str, event_id: str, summary: str) -> None:
        ev = self.events[(calendar_id, event_id)]
        ev["summary"] = summary
        self._bump(ev)

    def user_delete(self, calendar_id: str, event_id: str) -> None:
        ev = self.events[(calendar_id, event_id)]
        ev["status"] = "cancelled"
        self._bump(ev)

    def user_purge(self, calendar_id: str, event_id: str) -> None:
        """Gone without a trace: neither listed nor found (404 on events.get)."""
        self.events.pop((calendar_id, event_id))

    def _read(self, request: httpx.Request, calendar_id: str, event_id: str) -> httpx.Response:
        params = request.url.params
        fields = params.get("fields", "")
        # The ingest must never ask for summaries or descriptions (privacy, rule 4).
        assert "summary" not in fields and "description" not in fields and fields, fields
        if calendar_id in self.gone_calendars:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if event_id:
            ev = self.events.get((calendar_id, event_id))
            if ev is None:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json=_project_fields(ev))
        assert params.get("showDeleted") == "true" and params.get("singleEvents") == "true"
        lo, hi = params["timeMin"], params["timeMax"]
        items = [
            _project_fields(ev)
            for (cal, _), ev in sorted(self.events.items())
            if cal == calendar_id and ev["end"]["dateTime"] > lo and ev["start"]["dateTime"] < hi
        ]
        start = int(params.get("pageToken", "0"))
        page = items[start : start + self.list_page_size]
        body: dict[str, Any] = {"kind": "calendar#events", "items": page}
        if start + self.list_page_size < len(items):
            body["nextPageToken"] = str(start + self.list_page_size)
        return httpx.Response(200, json=body)

    def calendar_events(self, calendar_id: str, *, include_cancelled: bool = False) -> dict:
        return {
            eid: ev
            for (cal, eid), ev in self.events.items()
            if cal == calendar_id and (include_cancelled or ev["status"] != "cancelled")
        }

    def _event(self, request: httpx.Request, body: Any) -> httpx.Response:
        rest = request.url.path[len(CALENDARS_PATH) + 1 :]
        calendar_id, _, event_part = rest.partition("/events")
        event_id = event_part.lstrip("/") or (body or {}).get("id", "")
        if self.event_fault is not None:
            reply = self.event_fault(request.method, event_id)
            if reply is not None:
                if reply.raise_exc is not None:
                    raise reply.raise_exc
                return httpx.Response(reply.status, json=reply.json)
        if calendar_id in self.gone_calendars:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if request.method == "GET":
            return self._read(request, calendar_id, event_id)
        key = (calendar_id, event_id)
        existing = self.events.get(key)
        if request.method == "POST":
            if existing is not None:
                return httpx.Response(409, json={"error": {"code": 409, "message": "duplicate"}})
            return self._store(key, body)
        if request.method == "PUT":
            if existing is None:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            return self._store(key, {"id": event_id, **body})
        if request.method == "DELETE":
            if existing is None:
                return httpx.Response(404)
            if existing["status"] == "cancelled":
                return httpx.Response(410)
            existing["status"] = "cancelled"
            return httpx.Response(204)
        return httpx.Response(405)

    def _store(self, key: tuple[str, str], body: dict[str, Any]) -> httpx.Response:
        self._etags += 1
        event = {"kind": "calendar#event", **body, "etag": f'"{self._etags:06d}"'}
        event.setdefault("status", "confirmed")
        self.events[key] = event
        return httpx.Response(200, json=event)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, url, body, request.headers.get("authorization")))
        if request.method == "POST" and url == FREEBUSY_URL:
            reply = self.freebusy or Reply(json=busy_body(*self.busy))
        elif request.method == "POST" and url == CALENDARS_URL:
            if self.on_create is not None:
                await self.on_create()
            reply = self.create or Reply(
                json={"kind": "calendar#calendar", "id": self.ids.pop(0), "summary": "x"}
            )
        elif url.startswith(CALENDARS_URL + "/") and "/events" in request.url.path:
            self.event_started.set()
            if self.event_gate is not None:
                await self.event_gate.wait()
            return self._event(request, body)
        elif request.method == "DELETE" and url.startswith(CALENDARS_URL + "/"):
            reply = self.delete
        else:
            return httpx.Response(404)
        if reply.raise_exc is not None:
            raise reply.raise_exc
        if reply.json is None:
            return httpx.Response(reply.status)
        return httpx.Response(reply.status, json=reply.json)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _project_fields(ev: dict[str, Any]) -> dict[str, Any]:
    """What `fields=id,etag,status,start,end,extendedProperties/private` returns."""
    out = {k: ev[k] for k in ("id", "etag", "status", "start", "end") if k in ev}
    private = (ev.get("extendedProperties") or {}).get("private")
    if private is not None:
        out["extendedProperties"] = {"private": private}
    return out


def new_cipher() -> TokenCipher:
    return TokenCipher([(1, Fernet.generate_key().decode())])


def runtime(
    google: FakeGoogle, calendar: FakeCalendar, cipher: TokenCipher, clock: Clock
) -> GcalRuntime:
    gsettings = settings(calendar_api_url=CALENDAR_URL)
    return build_gcal_runtime(
        gsettings,
        cipher=cipher,
        client=HttpGoogleOAuthClient(gsettings, transport=google.transport()),
        calendar=HttpGoogleCalendarClient(gsettings, transport=calendar.transport()),
        clock=clock,
    )


def install(
    app: FastAPI, google: FakeGoogle, calendar: FakeCalendar, cipher: TokenCipher, clock: Clock
) -> GcalRuntime:
    rt = runtime(google, calendar, cipher, clock)
    app.state.gcal_runtime = rt
    return rt


async def seed_connection(
    maker: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    cipher: TokenCipher,
    *,
    status: str = "active",
    app_calendar_id: str | None = None,
    now: datetime,
) -> None:
    async with maker() as s, s.begin():
        await s.execute(
            insert(GoogleConnection).values(
                user_id=uid,
                google_email=EMAIL,
                refresh_token_enc=cipher.encrypt(REFRESH_TOKEN),
                key_id=cipher.primary_key_id,
                scopes=sorted(CALENDAR_SCOPES),
                app_calendar_id=app_calendar_id,
                status=status,
                revoked_at=now if status == "revoked" else None,
                created_at=now,
            )
        )

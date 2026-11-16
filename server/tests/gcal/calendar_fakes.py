"""Recorded-HTTP Google Calendar for T4.2 tests (freeBusy, calendars.insert/delete) plus helpers
that seed a Google connection row and install a gcal runtime. Nothing here talks to Google."""

from __future__ import annotations

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

    @property
    def freebusy_bodies(self) -> list[Any]:
        return [body for m, url, body, _ in self.requests if url == FREEBUSY_URL]

    @property
    def created(self) -> int:
        return sum(1 for m, url, _, _ in self.requests if m == "POST" and url == CALENDARS_URL)

    @property
    def deleted(self) -> list[str]:
        prefix = CALENDARS_URL + "/"
        return [
            httpx.URL(url).path.rsplit("/", 1)[1]
            for m, url, _, _ in self.requests
            if m == "DELETE" and url.startswith(prefix)
        ]

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

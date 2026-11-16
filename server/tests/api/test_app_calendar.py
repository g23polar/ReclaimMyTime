"""T4.2 `ensure_app_calendar`: lazy create, idempotent, concurrent creators leave one calendar
(the loser deletes its orphan via the API), 404 → clear + recreate. Postgres + recorded HTTP."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import unquote

import pytest
from sqlalchemy import text

from app.gcal.app_calendar import AppCalendarService
from app.gcal.errors import (
    GcalConnectionInactiveError,
    GcalGrantRevokedError,
    GcalTransientError,
)
from tests.api.conftest import NOW
from tests.db.conftest import requires_db, rows
from tests.gcal.calendar_fakes import CALENDARS_URL, runtime, seed_connection
from tests.gcal.fakes import Reply

pytestmark = requires_db

CAL1, CAL2 = "cal1@group.calendar.google.com", "cal2@group.calendar.google.com"


@pytest.fixture
def svc(maker, google, cal, cipher, clock) -> AppCalendarService:
    return runtime(google, cal, cipher, clock).app_calendar(maker)


async def stored(maker: Any, uid: uuid.UUID) -> Any:
    got = await rows(
        maker, "SELECT app_calendar_id FROM google_connections WHERE user_id = :u", u=uid
    )
    return got[0][0] if got else "<no row>"


def deleted(cal: Any) -> list[str]:
    return [unquote(d) for d in cal.deleted]


async def test_creates_once_then_is_idempotent(svc, maker, user_id, google, cal, cipher):
    await seed_connection(maker, user_id, cipher, now=NOW)
    assert await svc.ensure_app_calendar(user_id) == CAL1
    assert await stored(maker, user_id) == CAL1
    [(method, url, body, _)] = cal.requests
    assert (method, url, body) == ("POST", CALENDARS_URL, {"summary": "ReclaimMyTime"})
    minted = len(google.requests)
    assert await svc.ensure_app_calendar(user_id) == CAL1
    assert cal.created == 1 and len(google.requests) == minted  # no Google call at all


async def test_concurrent_creators_keep_one_and_delete_the_orphan(svc, maker, user_id, cal, cipher):
    await seed_connection(maker, user_id, cipher, now=NOW)
    arrived, both = 0, asyncio.Event()

    async def rendezvous() -> None:  # both creates are in flight before either stores
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)

    cal.on_create = rendezvous
    got = await asyncio.gather(svc.ensure_app_calendar(user_id), svc.ensure_app_calendar(user_id))
    kept = await stored(maker, user_id)
    assert kept in (CAL1, CAL2) and list(got) == [kept, kept]
    assert cal.created == 2
    assert deleted(cal) == [CAL2 if kept == CAL1 else CAL1]


async def test_orphan_delete_failure_still_returns_the_stored_calendar(
    svc, maker, user_id, cal, cipher
):
    await seed_connection(maker, user_id, cipher, now=NOW)

    async def someone_else_stores_first() -> None:
        async with maker() as s, s.begin():
            await s.execute(text("UPDATE google_connections SET app_calendar_id = 'theirs'"))

    cal.on_create = someone_else_stores_first
    cal.delete = Reply(status=500, json={})
    assert await svc.ensure_app_calendar(user_id) == "theirs"
    assert deleted(cal) == [CAL1]  # attempted; failure is best effort


async def test_404_clears_and_recreates(svc, maker, user_id, cal, cipher):
    await seed_connection(maker, user_id, cipher, app_calendar_id="old@group", now=NOW)
    assert await svc.ensure_app_calendar(user_id) == "old@group"
    assert cal.created == 0
    assert await svc.ensure_app_calendar(user_id, missing="old@group") == CAL1
    assert await stored(maker, user_id) == CAL1


async def test_a_late_404_for_an_older_calendar_keeps_the_current_one(
    svc, maker, user_id, cal, cipher
):
    await seed_connection(maker, user_id, cipher, app_calendar_id="current@group", now=NOW)
    assert await svc.ensure_app_calendar(user_id, missing="old@group") == "current@group"
    assert cal.requests == []


async def test_not_connected_or_revoked_is_inactive(svc, maker, user_id, cal, cipher):
    with pytest.raises(GcalConnectionInactiveError):
        await svc.ensure_app_calendar(user_id)
    await seed_connection(maker, user_id, cipher, status="revoked", now=NOW)
    with pytest.raises(GcalConnectionInactiveError):
        await svc.ensure_app_calendar(user_id)
    assert cal.requests == []


async def test_invalid_grant_while_minting(svc, maker, user_id, google, cal, cipher):
    await seed_connection(maker, user_id, cipher, now=NOW)
    google.refresh = Reply(status=400, json={"error": "invalid_grant"})
    with pytest.raises(GcalGrantRevokedError):
        await svc.ensure_app_calendar(user_id)
    assert cal.requests == [] and await stored(maker, user_id) is None


async def test_create_failure_is_transient_and_stores_nothing(svc, maker, user_id, cal, cipher):
    await seed_connection(maker, user_id, cipher, now=NOW)
    cal.create = Reply(status=503, json={})
    with pytest.raises(GcalTransientError):
        await svc.ensure_app_calendar(user_id)
    assert await stored(maker, user_id) is None


async def test_disconnect_during_create_deletes_the_orphan(svc, maker, user_id, cal, cipher):
    await seed_connection(maker, user_id, cipher, now=NOW)

    async def disconnected() -> None:
        async with maker() as s, s.begin():
            await s.execute(text("DELETE FROM google_connections"))

    cal.on_create = disconnected
    with pytest.raises(GcalConnectionInactiveError):
        await svc.ensure_app_calendar(user_id)
    assert deleted(cal) == [CAL1]

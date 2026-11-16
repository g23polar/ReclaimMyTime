"""T4.3a, same discipline as test_busy_pool.py: the write-back never holds a pooled connection
across a Google call, never holds two at once, and never waits on the per-user lock while
holding a session. The app runs on an engine with exactly ONE pooled connection and a short
checkout timeout; confirm (which kicks a background drain), the drain itself and
`POST /gcal/sync` (which joins it) all share that connection. A drain or request that kept a
session open across Google, or opened a second one, would time out the pool (a 500 or a
failed drain) instead of finishing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tests.api.conftest import NOW, live_blocks
from tests.api.test_busy_routes import BUSY, WEDNESDAY_OFF, ok
from tests.db.conftest import DATABASE_URL, WORKOUT, requires_db
from tests.gcal.calendar_fakes import seed_connection

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]

POOL_TIMEOUT = 2.0


@pytest.fixture
async def one_connection(schema: str, engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL
    tiny = create_async_engine(
        DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        yield tiny
    finally:
        await tiny.dispose()


@pytest.fixture
def tiny_app(app: FastAPI, one_connection: AsyncEngine) -> FastAPI:
    app.state.sessionmaker = async_sessionmaker(one_connection, expire_on_commit=False)
    return app


async def test_confirm_kick_and_sync_share_one_connection(
    tiny_app, client, cal, cipher, maker, who, caplog
):
    caplog.set_level(logging.INFO, logger="app.gcal.writeback")

    async def flow() -> None:
        await seed_connection(maker, who.user_id, cipher, now=NOW)
        cal.busy = list(BUSY)
        started = time.monotonic()

        first = await ok(client.post("/gcal/sync"), 200)  # creates the calendar, writes all
        assert first["outbox"]["pending"] == 0

        # Confirm kicks a drain; a sync immediately after joins it; another write path and
        # confirm run while that drain may still be going.
        staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
        await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
        mid = await ok(client.post("/gcal/sync"), 200)
        assert mid["pending_changeset"] is None
        edit = await ok(client.patch(f"/projects/{WORKOUT}", json={"weekly_target_hours": 2.0}))
        kicked = client.post(f"/changesets/{edit['id']}/confirm")
        synced = client.post("/gcal/sync")
        confirm_resp, sync_resp = await asyncio.gather(kicked, synced)
        assert confirm_resp.status_code == 200, confirm_resp.text
        assert sync_resp.status_code == 200, sync_resp.text

        await tiny_app.state.gcal_outbox.idle()
        final = await ok(client.post("/gcal/sync"), 200)
        assert final["outbox"]["pending"] == 0
        assert {b.gcal_status for b in await live_blocks(maker, who.user_id)} == {"synced"}
        # A pool wait would have cost POOL_TIMEOUT and surfaced as a 500 or a failed drain.
        assert time.monotonic() - started < 30
        outcomes = [r.getMessage() for r in caplog.records if r.name == "app.gcal.writeback"]
        assert outcomes and all(m.startswith("gcal write-back: done") for m in outcomes), outcomes

    # A held lock or connection must fail the test, never hang the suite.
    await asyncio.wait_for(flow(), 60)

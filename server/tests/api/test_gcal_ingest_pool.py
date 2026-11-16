"""T4.3b, same discipline as test_busy_pool.py / test_gcal_writeback_pool.py: the ingest and the
drift check never hold a pooled connection across a Google call (events.list / events.get /
freeBusy / token mint), never hold two at once, and never wait on the per-user lock while
holding a session. The app runs on an engine with exactly ONE pooled connection and a short
checkout timeout; a sync that kept a session open across Google, or opened a second one under
the lock, would time out the pool (a 500) instead of finishing. Every flow is
`asyncio.wait_for`-guarded: a held lock must fail the test, never hang the suite."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tests.api.test_busy_routes import WEDNESDAY_OFF, ok
from tests.api.test_gcal_ingest import CAL1, block_at, eid, make_world
from tests.db.conftest import DATABASE_URL, at, requires_db

pytestmark = [requires_db, pytest.mark.usefixtures("gcal")]

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


async def test_ingest_drift_and_reject_share_one_connection(
    tiny_app, client, maker, who, cipher, clock, cal
):
    async def flow() -> None:
        await make_world(maker, who.user_id, cipher, clock, client)  # syncs on the tiny pool
        started = time.monotonic()
        wed = await block_at(maker, who.user_id, 2, "08:00")
        fri = await block_at(maker, who.user_id, 4, "08:00")
        cal.user_move(CAL1, eid(fri), at(4, "08:00"), at(4, "08:30"))  # (ii): applied
        cal.user_move(CAL1, eid(wed), at(3, "08:30"), at(3, "09:30"))  # (iii): staged
        cal.busy = [(at(5, "07:30"), at(5, "09:00"))]  # drift on Saturday (not proposed:
        # the google_edit changeset is pending by then)

        body = await ok(client.post("/gcal/sync"), 200)
        cs = body["pending_changeset"]
        assert cs["reason"] == "google_edit"

        # A reject (kicks a drain) racing a sync, then a staged write racing another sync.
        rejected, synced = await asyncio.gather(
            client.post(f"/changesets/{cs['id']}/reject"), client.post("/gcal/sync")
        )
        assert rejected.status_code == 200, rejected.text
        assert synced.status_code == 200, synced.text
        await tiny_app.state.gcal_outbox.idle()
        staged, synced = await asyncio.gather(
            client.post("/exceptions", json=WEDNESDAY_OFF), client.post("/gcal/sync")
        )
        assert staged.status_code == 202, staged.text
        assert synced.status_code == 200, synced.text
        await tiny_app.state.gcal_outbox.idle()
        final = await ok(client.post("/gcal/sync"), 200)
        assert final["outbox"]["pending"] == 0
        # A pool wait would have cost POOL_TIMEOUT per checkout and surfaced as a 500.
        assert time.monotonic() - started < 30

    await asyncio.wait_for(flow(), 60)


async def test_drift_on_one_connection(tiny_app, client, maker, who, cipher, clock, cal):
    async def flow() -> None:
        await make_world(maker, who.user_id, cipher, clock, client)
        cal.busy = [(at(3, "07:30"), at(3, "09:00"))]
        first, second = await asyncio.gather(client.post("/gcal/sync"), client.post("/gcal/sync"))
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        ids = {r.json()["pending_changeset"]["id"] for r in (first, second)}
        assert len(ids) == 1  # serialized under the lock: one drift proposal, not two
        assert (await ok(client.post("/gcal/sync"), 200))["pending_changeset"]["id"] in ids

    await asyncio.wait_for(flow(), 60)

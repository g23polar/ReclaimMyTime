"""T4.2 review major 3: no request holds a pooled connection across a Google call (and none
holds two at once). The app runs on an engine with exactly ONE pooled connection and a short
checkout timeout. A request that kept its preview/request transaction open while the busy
provider opened its own session would wait for the pool and fail with a 500 after
`pool_timeout`. The whole connected flow runs end to end: staging routes, PATCH /blocks, confirm
(including a stale re-stage), and an agent turn."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.router import router as agent_router
from tests.agent.fake_anthropic import FakeAnthropic
from tests.api.conftest import NOW, future_block
from tests.api.test_agent_message import turn
from tests.api.test_busy_agent import PROPOSE
from tests.api.test_busy_routes import BUSY, WEDNESDAY_OFF, ok, overlaps
from tests.db.conftest import DATABASE_URL, WORKOUT, requires_db
from tests.gcal.calendar_fakes import seed_connection, z

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]

POOL_TIMEOUT = 2.0


@pytest.fixture
async def one_connection(schema: str, engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Same migrated schema as `engine` (which this depends on), but a pool of one."""
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
    app.include_router(agent_router)
    fake = FakeAnthropic()
    fake.script = list(PROPOSE)
    app.state.anthropic_client = fake.client()
    return app


async def test_connected_flow_never_needs_a_second_connection(
    tiny_app, client, cal, cipher, maker, who
):
    async def flow() -> None:
        await seed_connection(maker, who.user_id, cipher, now=NOW)
        cal.busy = list(BUSY)
        started = time.monotonic()

        # Staging routes (validation pass → busy read → write pass), then confirm.
        staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
        await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
        await ok(client.patch(f"/projects/{WORKOUT}", json={"weekly_target_hours": 2.0}))

        # PATCH /blocks (locks in both passes).
        block = await future_block(maker, who.user_id)
        edited = await ok(client.patch(f"/blocks/{block.id}", json={"action": "delete"}))
        assert not any(overlaps(b["span"], *BUSY[0]) for b in edited["plan"]["blocks"])

        # Confirm that goes stale (a meeting lands on a proposed block) and re-stages.
        first = next(b for b in edited["plan"]["blocks"] if b["span"]["start"] >= z(NOW))
        cal.busy = [*BUSY, tuple(map(_dt, (first["span"]["start"], first["span"]["end"])))]
        stale = await client.post(f"/changesets/{edited['id']}/confirm")
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "changeset_stale"

        # An agent turn: dry run, then commit, each reading busy time outside their sessions.
        events = await turn(client, ("user", "No laptop today or tomorrow."))
        assert events[-1]["event"] == "done", events[-1]

        # A pool wait would have cost POOL_TIMEOUT and surfaced as a 500 above.
        assert time.monotonic() - started < 30

    # A held lock or connection must fail the test, never hang the suite.
    await asyncio.wait_for(flow(), 60)


def _dt(value: str):  # type: ignore[no-untyped-def]
    from datetime import datetime

    return datetime.fromisoformat(value)


async def test_the_pool_of_one_really_detects_a_held_connection(one_connection):
    """Self-check: holding the only connection makes a second checkout time out."""
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(one_connection)
    async with maker() as held, held.begin():
        await held.connection()
        with pytest.raises(Exception, match="QueuePool limit|timed out"):
            async with maker() as other, other.begin():
                await other.connection()

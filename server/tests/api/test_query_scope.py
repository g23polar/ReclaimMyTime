"""Plan responses read only the plans they render: no history-wide aggregate (the
completed-minutes ranking over every live/superseded plan) unless a `reduce_estimate`
relaxation needs it, and every blocks SELECT is filtered by plan_id."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.db.conftest import WORKOUT, requires_db

pytestmark = requires_db


@contextmanager
def captured(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def assert_scoped(statements: list[str]) -> None:
    assert not [s for s in statements if "row_number" in s], "history-wide ranking ran"
    block_reads = [s for s in statements if re.match(r"SELECT .* FROM blocks\b", s)]
    assert block_reads
    for s in block_reads:
        assert "blocks.plan_id =" in s or "blocks.plan_id IN" in s, s


async def test_current_plan_reads_only_the_live_plan(
    client: httpx.AsyncClient, engine: AsyncEngine, world: uuid.UUID
) -> None:
    for _ in range(3):  # retained history: several confirmed versions
        cs = (await client.patch(f"/projects/{WORKOUT}", json={"max_block_minutes": 45})).json()
        assert (await client.post(f"/changesets/{cs['id']}/confirm")).status_code == 200
    with captured(engine) as statements:
        assert (await client.get("/plans/current")).status_code == 200
    assert_scoped(statements)


async def test_changeset_responses_read_only_proposal_and_against_plan(
    client: httpx.AsyncClient, engine: AsyncEngine, world: uuid.UUID
) -> None:
    staged = (await client.patch(f"/projects/{WORKOUT}", json={"max_block_minutes": 45})).json()
    with captured(engine) as statements:
        resp = await client.post(f"/changesets/{staged['id']}/reject")
    assert resp.status_code == 200
    assert_scoped(statements)

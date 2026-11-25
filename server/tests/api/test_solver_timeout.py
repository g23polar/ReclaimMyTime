"""Issue #22: every server-side solve runs off the event loop under SOLVER_TIMEOUT_SECONDS. A
solve that runs out of time is 503 `solver_timeout` (an in-stream `error` on the agent route)
and its transaction rolls back: no changeset, plan, block, mutation or pin from that request
is committed. A slow solve never blocks other requests on the same loop.

A slow solve is simulated by patching the `pack` the helper calls with a wrapper that sleeps
and then delegates; the scheduler itself is untouched."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.db import solve as module
from app.main import health
from app.scheduler import pack
from tests.agent.fake_anthropic import FakeAnthropic
from tests.api.contract import assert_conforms
from tests.api.test_agent_message import agent_turns, changesets, turn
from tests.api.test_busy_agent import PROPOSE, STAGE_ONLY, agent_app, fake  # noqa: F401  (pytest fixtures)
from tests.api.test_busy_routes import WEDNESDAY_OFF, ok
from tests.api.test_gcal_ingest import (
    CAL1,
    block_at,
    eid,
    live_plan_id,
    settle,
    stable,  # noqa: F401  (pytest fixture)
    sync,
)
from tests.db.conftest import WORKOUT, at, requires_db, rows

pytestmark = [requires_db, pytest.mark.usefixtures("gcal")]

SLEEP = 1.5  # the slow solve; the timeout below is far shorter
TIMEOUT = "0.2"


def slow_solves(monkeypatch: Any, *, from_call: int = 1, sleep: float = SLEEP) -> list[int]:
    """Solves numbered `from_call` and later sleep `sleep` s before delegating to `pack`.
    Returns the call log (one entry per solve)."""
    calls: list[int] = []

    def slow(*args: Any) -> Any:
        calls.append(len(calls) + 1)
        if len(calls) >= from_call:
            time.sleep(sleep)
        return pack(*args)

    monkeypatch.setattr(module, "pack", slow)
    return calls


async def snapshot(maker: Any) -> dict[str, list[Any]]:
    """Every row a solving request could write, by identity and status."""
    return {
        "changesets": await rows(maker, "SELECT id, status::text FROM changesets ORDER BY id"),
        "week_plans": await rows(maker, "SELECT id, status::text FROM week_plans ORDER BY id"),
        # Not `state`: reads lazily sweep ended blocks to `completed` in their own transaction.
        "blocks": await rows(maker, "SELECT id, plan_id, span::text FROM blocks ORDER BY id"),
        "mutations": await rows(maker, "SELECT id FROM mutations ORDER BY id"),
        "block_pins": await rows(maker, "SELECT id FROM block_pins ORDER BY id"),
        "exceptions": await rows(maker, "SELECT id FROM availability_exceptions ORDER BY id"),
    }


def assert_solver_timeout(body: dict[str, Any]) -> None:
    assert body == {"error": {"code": "solver_timeout", "message": body["error"]["message"]}}
    assert "nothing was changed" in body["error"]["message"]


async def status(maker: Any, cs: str) -> str:
    got = await rows(maker, "SELECT status::text FROM changesets WHERE id = :c", c=uuid.UUID(cs))
    return got[0][0]


# ---------------------------------------------------------------- REST staging


@pytest.mark.usefixtures("world")
async def test_staging_timeout_is_503_and_stages_nothing(client, maker, monkeypatch):
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch)
    before = await snapshot(maker)
    started = time.monotonic()
    resp = await client.post("/exceptions", json=WEDNESDAY_OFF)
    assert time.monotonic() - started < SLEEP  # answered at the timeout, not after the solve
    assert resp.status_code == 503, resp.text
    assert_conforms("createException", 503, resp.json())
    assert_solver_timeout(resp.json())
    assert calls == [1]
    assert await snapshot(maker) == before


@pytest.mark.usefixtures("world")
async def test_health_answers_while_a_solve_runs(app: FastAPI, client, monkeypatch):
    app.add_api_route("/health", health)
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", "30")
    loop = asyncio.get_running_loop()
    running = asyncio.Event()

    def slow(*args: Any) -> Any:
        loop.call_soon_threadsafe(running.set)
        time.sleep(2.0)
        return pack(*args)

    monkeypatch.setattr(module, "pack", slow)
    staging = asyncio.create_task(client.post("/exceptions", json=WEDNESDAY_OFF))
    await asyncio.wait_for(running.wait(), 10)
    resp = await client.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    assert not staging.done()  # /health answered while the solve was still running
    assert (await staging).status_code == 202


# ---------------------------------------------------------------- confirm


@pytest.mark.usefixtures("world")
async def test_confirm_stale_check_timeout_is_503_and_promotes_nothing(client, maker, monkeypatch):
    cs = (await ok(client.post("/exceptions", json=WEDNESDAY_OFF)))["id"]
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch)
    before = await snapshot(maker)
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 503, resp.text
    assert_conforms("confirmChangeset", 503, resp.json())
    assert_solver_timeout(resp.json())
    assert calls == [1]  # the same week: `_check_current_week` does not solve
    assert await status(maker, cs) == "pending"
    assert await snapshot(maker) == before


@pytest.mark.usefixtures("world")
async def test_confirm_after_the_week_ended_times_out_with_nothing_applied(
    client, clock, maker, monkeypatch
):
    cs = (await ok(client.patch(f"/projects/{WORKOUT}", json={"weekly_target_hours": 2.0})))["id"]
    clock.current = at(7, "06:00")  # next Monday 06:00 local: the proposal's week has ended
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch)
    before = await snapshot(maker)
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 503, resp.text
    assert_solver_timeout(resp.json())
    assert calls == [1]  # `_check_current_week`'s solve; nothing was restaged
    assert await status(maker, cs) == "pending"
    assert await snapshot(maker) == before


# ---------------------------------------------------------------- POST /gcal/sync


async def test_sync_per_edit_solve_timeout_applies_no_edit(
    app,
    stable,  # noqa: F811
    client,
    maker,
    cal,
    monkeypatch,
):
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(6, "10:00"), at(6, "11:00"))  # case (i) if it solved
    await settle(app)
    plan = await live_plan_id(maker)
    before = await snapshot(maker)
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch)
    assert_solver_timeout(await sync(client, 503))
    assert calls == [1]
    assert await live_plan_id(maker) == plan
    assert (await block_at(maker, stable, 2, "08:00")).lineage_id == wed.lineage_id
    assert await snapshot(maker) == before


async def test_sync_follow_up_timeout_rolls_back_the_direct_apply(
    app,
    stable,  # noqa: F811
    client,
    maker,
    cal,
    monkeypatch,
):
    """Case (ii): the per-edit solve succeeds and the shrunk block is promoted directly; the
    follow-up solve for the knock-on proposal times out, so the direct apply rolls back too."""
    wed = await block_at(maker, stable, 2, "08:00")
    cal.user_move(CAL1, eid(wed), at(2, "08:00"), at(2, "08:30"))
    await settle(app)
    plan = await live_plan_id(maker)
    before = await snapshot(maker)
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch, from_call=2)
    assert_solver_timeout(await sync(client, 503))
    assert calls == [1, 2]
    assert await live_plan_id(maker) == plan
    assert (await block_at(maker, stable, 2, "08:00")).span.upper == wed.span.upper
    assert await snapshot(maker) == before


# ---------------------------------------------------------------- POST /agent/message


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize("script", [PROPOSE, STAGE_ONLY], ids=["dry_run", "commit"])
async def test_agent_solve_timeout_is_one_in_stream_error_with_nothing_staged(
    agent_app,  # noqa: F811
    client,
    fake: FakeAnthropic,  # noqa: F811
    maker,
    who,
    monkeypatch,
    script,
):
    fake.script = list(script)
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", TIMEOUT)
    calls = slow_solves(monkeypatch)
    before = await snapshot(maker)
    events = await turn(client, ("user", "No laptop today or tomorrow."))
    assert [e for e in events if e["event"] == "error"] == [events[-1]]
    assert events[-1]["event"] == "error"
    assert_solver_timeout(events[-1]["data"])
    assert calls == [1]
    assert not [c for c in await changesets(maker, who.user_id) if c[1] == "pending"]
    [trace] = await agent_turns(maker, who.user_id)
    assert trace[:2] == ("error", "solver_timeout")
    assert await snapshot(maker) == before

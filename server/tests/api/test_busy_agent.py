"""T4.2 on `POST /agent/message`: the turn reads busy time when it first solves (dry run), reuses
it for the commit, and a Google failure ends the stream with an in-stream `error` carrying the
REST code (`gcal_unavailable` / `gcal_reconnect_required`) and nothing staged."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.agent.router import router as agent_router
from tests.agent.fake_anthropic import Call, FakeAnthropic, Text, Tool
from tests.api.conftest import NOW
from tests.api.test_agent_message import LAPTOP, changesets, turn
from tests.api.test_busy_routes import BUSY, overlaps
from tests.db.conftest import requires_db
from tests.gcal.calendar_fakes import seed_connection
from tests.gcal.fakes import Reply

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]

PROPOSE = [
    Call([Tool("stage_exception_add", LAPTOP)], "tool_use"),
    Call([Tool("propose_schedule", {})], "tool_use"),
    Call([Text("Here's the new week.")]),
]
STAGE_ONLY = [
    Call([Tool("stage_exception_add", LAPTOP)], "tool_use"),
    Call([Text("Done.")]),
]


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def agent_app(app: FastAPI, fake: FakeAnthropic) -> FastAPI:
    app.include_router(agent_router)
    app.state.anthropic_client = fake.client()
    return app


async def test_busy_reaches_the_agent_solve_and_is_read_once(
    agent_app, client, fake, cal, cipher, maker, who
):
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cal.busy = list(BUSY)
    fake.script = list(PROPOSE)
    done = (await turn(client, ("user", "No laptop today or tomorrow.")))[-1]
    assert done["event"] == "done", done
    blocks = done["data"]["changeset"]["plan"]["blocks"]
    assert blocks and not any(overlaps(b["span"], *BUSY[0]) for b in blocks)
    # One read for the turn: the dry run and the commit solve against identical busy time.
    assert [b["items"] for b in cal.freebusy_bodies] == [[{"id": "primary"}]]


@pytest.mark.parametrize("script", [PROPOSE, STAGE_ONLY], ids=["dry_run", "commit"])
@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("transient", "gcal_unavailable"),
        ("revoked", "gcal_reconnect_required"),
        ("invalid_grant", "gcal_reconnect_required"),
    ],
)
async def test_google_failure_is_an_in_stream_error_with_nothing_staged(
    agent_app, client, fake, google, cal, cipher, maker, who, script, setup, code
):
    status = "revoked" if setup == "revoked" else "active"
    await seed_connection(maker, who.user_id, cipher, status=status, now=NOW)
    if setup == "transient":
        cal.freebusy = Reply(status=503, json={})
    elif setup == "invalid_grant":
        google.refresh = Reply(status=400, json={"error": "invalid_grant"})
    fake.script = list(script)
    before = await changesets(maker, who.user_id)
    events = await turn(client, ("user", "No laptop today or tomorrow."))
    assert events[-1] == {
        "event": "error",
        "data": {"error": {"code": code, "message": events[-1]["data"]["error"]["message"]}},
    }
    assert await changesets(maker, who.user_id) == before

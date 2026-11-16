"""`Changeset.reason` (T4.3 contract, migration 0002) on every path that creates a changeset:
REST staging → `user_edit`, an agent turn → `agent`, a stale re-proposal keeps the reason of
the changeset it replaces; `drift` is the only reason allowed an empty `staged` list."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI

from app.agent.router import router as agent_router
from app.api.views import changeset_view
from app.db.write_paths import InvalidMutation, propose_changeset
from tests.agent.fake_anthropic import FakeAnthropic
from tests.api.conftest import NOW
from tests.api.contract import assert_conforms
from tests.api.test_agent_message import turn
from tests.api.test_busy_agent import PROPOSE
from tests.api.test_busy_routes import WEDNESDAY_OFF, ok
from tests.db.conftest import requires_db, rows
from tests.gcal.calendar_fakes import seed_connection

pytestmark = [requires_db, pytest.mark.usefixtures("world", "gcal")]


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def agent_app(app: FastAPI, fake: FakeAnthropic) -> FastAPI:
    app.include_router(agent_router)
    app.state.anthropic_client = fake.client()
    return app


async def stored_reason(maker: Any, cs: str) -> str:
    got = await rows(maker, "SELECT reason::text FROM changesets WHERE id = :c", c=uuid.UUID(cs))
    return got[0][0]


async def test_rest_staging_is_user_edit_and_confirm_keeps_it(client, maker):
    staged = await ok(client.post("/exceptions", json=WEDNESDAY_OFF))
    assert staged["reason"] == "user_edit"
    done = await ok(client.post(f"/changesets/{staged['id']}/confirm"), 200)
    assert_conforms("confirmChangeset", 200, done)
    assert done["reason"] == "user_edit" == await stored_reason(maker, staged["id"])


async def test_an_agent_turn_is_agent(agent_app, client, fake, maker):
    fake.script = list(PROPOSE)
    done = (await turn(client, ("user", "No laptop today or tomorrow.")))[-1]
    assert done["event"] == "done", done
    changeset = done["data"]["changeset"]
    assert changeset["reason"] == "agent" == await stored_reason(maker, changeset["id"])


def _first_future(body: dict[str, Any]) -> tuple[datetime, datetime]:
    spans = sorted(
        (datetime.fromisoformat(b["span"]["start"]), datetime.fromisoformat(b["span"]["end"]))
        for b in body["plan"]["blocks"]
    )
    return next(s for s in spans if s[0] >= NOW)


async def test_a_stale_re_proposal_keeps_the_agent_reason(
    agent_app, client, fake, cal, cipher, maker, who
):
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    fake.script = list(PROPOSE)
    changeset = (await turn(client, ("user", "No laptop today or tomorrow.")))[-1]["data"][
        "changeset"
    ]
    cal.busy = [_first_future(changeset)]  # a meeting lands on a proposed block
    resp = await client.post(f"/changesets/{changeset['id']}/confirm")
    assert resp.status_code == 409, resp.text
    fresh = resp.json()["error"]["details"]["changeset_id"]
    assert await stored_reason(maker, fresh) == "agent"


async def test_only_drift_or_google_edit_may_stage_nothing(maker, who):
    async with maker() as s, s.begin():
        cs = await propose_changeset(s, who.user_id, [], now=NOW, source="system", reason="drift")
        view = await changeset_view(s, who.user_id, cs)
    assert (view["reason"], view["staged"]) == ("drift", [])
    assert view["plan"] is not None
    assert_conforms(
        "syncGcal", 200, {"pending_changeset": view, "outbox": {"pending": 0, "synced": 0}}
    )

    for kwargs in ({"source": "manual"}, {"source": "agent"}):
        with pytest.raises(InvalidMutation):
            async with maker() as s, s.begin():
                await propose_changeset(s, who.user_id, [], now=NOW, **kwargs)  # type: ignore[arg-type]

    # 2026-11-16 (T4.3b): a google_edit knock-on re-solve (its pin already applied) is empty.
    async with maker() as s, s.begin():
        cs = await propose_changeset(
            s, who.user_id, [], now=NOW, source="system", reason="google_edit"
        )
        view = await changeset_view(s, who.user_id, cs)
    assert (view["reason"], view["staged"]) == ("google_edit", [])
    assert_conforms(
        "syncGcal", 200, {"pending_changeset": view, "outbox": {"pending": 0, "synced": 0}}
    )


async def test_a_system_changeset_must_name_its_reason(maker, who):
    with pytest.raises(ValueError, match="explicit changeset reason"):
        async with maker() as s, s.begin():
            await propose_changeset(s, who.user_id, [], now=NOW, source="system")

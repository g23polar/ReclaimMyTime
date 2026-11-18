"""T3.2 `POST /agent/message` end to end: real routers + Postgres, mocked Anthropic."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from sqlalchemy import text

from app.agent.loop import StaleBase
from app.agent.router import DbProposer
from app.agent.router import router as agent_router
from app.agent.settings import AgentSettings
from app.gcal.busy import GcalBusy
from tests.agent.fake_anthropic import Call, Failure, FakeAnthropic, Hang, Text, Tool
from tests.api.conftest import NOW, new_user
from tests.api.contract import _FORMATS, _REGISTRY, BASE, assert_conforms, load_fixture
from tests.db.conftest import PORTFOLIO, at, exception_added, prefs, propose, rows, staged

pytestmark = pytest.mark.usefixtures("world")

_EVENT = Draft202012Validator(
    {"$ref": BASE + "#/components/schemas/AgentStreamEvent"},
    registry=_REGISTRY,
    format_checker=_FORMATS,
)


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def agent_app(app: FastAPI, fake: FakeAnthropic) -> FastAPI:
    app.include_router(agent_router)
    app.state.anthropic_client = fake.client()
    return app


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for frame in text.split("\n\n"):
        lines = [ln for ln in frame.splitlines() if not ln.startswith(":")]
        if not lines:
            continue
        name = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[6:] for ln in lines if ln.startswith("data: ")))
        events.append({"event": name, "data": data})
    return events


async def turn(client: httpx.AsyncClient, *messages: tuple[str, str]) -> list[dict[str, Any]]:
    body = {"messages": [{"role": r, "content": c} for r, c in messages]}
    res = await client.post("/agent/message", json=body)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(res.text)
    for e in events:
        errors = list(_EVENT.iter_errors(e))
        assert not errors, f"{e['event']}: {errors[0].message[:300]}"
    terminal = [e for e in events if e["event"] in ("done", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0]
    return events


async def changesets(maker, uid) -> list[tuple[Any, ...]]:
    return await rows(
        maker,
        "SELECT id, status::text, jsonb_array_length(staged) FROM changesets"
        " WHERE user_id = :u ORDER BY created_at",
        u=uid,
    )


LAPTOP = {
    "start": {"day": "2026-11-17", "time": "10:30"},
    "end": {"day": "2026-11-19", "time": "00:00"},
    "blocks_all": True,
    "blocks_tags": [],
    "reason": "no laptop",
    "interpretation": "No laptop from now through Wednesday night.",
}


# ---------------------------------------------------------------- pre-stream


async def test_last_message_not_user_is_422_matching_fixture(agent_app, client, fake):
    fx = load_fixture("post_agent_message__last_not_user.json")
    res = await client.post("/agent/message", json=fx["request"]["body"])
    assert res.status_code == 422
    assert res.json() == fx["response"]["body"]
    assert fake.requests == []  # never invoked without a user turn


async def test_invalid_body_is_422_without_model_call(agent_app, client, fake):
    res = await client.post("/agent/message", json={"messages": []})
    assert res.status_code == 422 and res.json()["error"]["code"] == "validation_failed"
    assert fake.requests == []


async def test_upstream_down_before_stream_is_503(agent_app, client, fake):
    fake.script = [Failure(529)]
    res = await client.post(
        "/agent/message", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------- full turn


async def test_canonical_laptop_turn(agent_app, client, fake, maker, who):
    fake.script = [
        Call([Text("Got it: no laptop through Wednesday."), Tool("stage_exception_add", LAPTOP)],
             "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("I moved things around. OK?")]),
    ]  # fmt: skip
    before = await changesets(maker, who.user_id)
    events = await turn(client, ("user", "I don't have my laptop today or tomorrow."))
    assert [e["event"] for e in events] == ["delta", "status", "status", "delta", "done"]
    done = events[-1]["data"]
    assert done["outcome"] == "completed"
    cs = done["changeset"]
    assert_conforms("confirmChangeset", 200, cs)  # same Changeset schema
    assert cs["status"] == "pending" and cs["plan"] is not None
    assert cs["narration"] is None  # agent proposals: the reply narrates (contract)
    [m] = cs["staged"]
    assert m["kind"] == "exception_added" and m["source"] == "agent"
    assert m["payload"]["span"] == {"start": "2026-11-17T18:30:00Z", "end": "2026-11-19T08:00:00Z"}

    # The dry run during propose_schedule wrote nothing: exactly one new changeset.
    after = await changesets(maker, who.user_id)
    assert len(after) == len(before) + 1 and after[-1][0] == uuid.UUID(cs["id"])
    # The model saw the solver's result as its tool result.
    solved = fake.requests[2]["messages"][-1]["content"][0]["content"]
    assert solved.startswith("Proposed schedule (")


async def test_snapshot_lists_labels_and_local_now(agent_app, client, fake):
    fake.script = [Call([Text("You have Workout and P.")])]
    await turn(client, ("user", "What's on my plate?"))
    snapshot = fake.requests[0]["messages"][-1]
    assert snapshot["role"] == "system"
    text = snapshot["content"]
    assert "Now: Tue 2026-11-17 10:30 (America/Los_Angeles)" in text
    assert "  P1 'P': active; deadline" in text
    assert "  P2 'Workout': active; perennial, 3.0h per week" in text
    assert "Tags: gym" in text
    assert "Live schedule from now on:" in text


async def test_auto_propose_replies_with_template_narration(agent_app, client, fake):
    fake.script = [
        Call([Tool("stage_exception_add", LAPTOP)], "tool_use"),
        Call([Text("Noted.")]),
    ]
    done = (await turn(client, ("user", "No laptop until Thursday.")))[-1]["data"]
    assert done["reply"].startswith("Here's the proposal: No laptop is blocked: ")
    assert done["changeset"]["status"] == "pending"


async def test_refusal_changes_nothing(agent_app, client, fake, maker, who):
    fx = load_fixture("post_agent_message__refusal.json")
    fake.script = [Call([], "refusal")]
    before = await changesets(maker, who.user_id)
    events = await turn(
        client, *[(m["role"], m["content"]) for m in fx["request"]["body"]["messages"]]
    )
    assert events == fx["response"]["body"]
    assert await changesets(maker, who.user_id) == before


async def test_upstream_error_mid_stream_stages_nothing(agent_app, client, fake, maker, who):
    fake.script = [
        Call([Text("Got it: no laptop"), Tool("stage_exception_add", LAPTOP)], "tool_use"),
        Failure(500),
    ]
    before = await changesets(maker, who.user_id)
    events = await turn(client, ("user", "I don't have my laptop today or tomorrow."))
    fx = load_fixture("post_agent_message__upstream_error.json")
    assert [e["event"] for e in events] == ["delta", "status", "error"]
    assert [e for e in events if e["event"] != "status"] == fx["response"]["body"]
    assert await changesets(maker, who.user_id) == before


# ---------------------------------------------------------------- pending changeset


async def test_turns_accumulate_on_the_pending_changeset(agent_app, client, fake, maker, who):
    piano = {
        "name": "Piano", "kind": "perennial", "target_date": None, "estimated_total_hours": None,
        "weekly_target_hours": 2, "max_block_minutes": 45, "min_block_minutes": None,
        "preferred_windows": None, "preferred_strict": False, "requirements": [],
        "new_tags": [], "interpretation": "Piano 2h a week.",
    }  # fmt: skip
    fake.script = [
        Call([Tool("stage_project_create", piano)], "tool_use"),
        Call([Text("Added piano.")]),
        Call([Tool("stage_pin_add", {
            "kind": "pin", "project": "P3",
            "start": {"day": "2026-11-18", "time": "18:00"},
            "end": {"day": "2026-11-18", "time": "18:45"},
            "interpretation": "Piano Wednesday at 6.",
        })], "tool_use"),
        Call([Text("Pinned.")]),
    ]  # fmt: skip
    first = (await turn(client, ("user", "Add piano 2h a week")))[-1]["data"]["changeset"]
    second = (
        await turn(
            client,
            ("user", "Add piano 2h a week"),
            ("assistant", "Added piano."),
            ("user", "Pin it Wednesday at 6"),
        )
    )[-1]["data"]["changeset"]
    # Turn 2 saw turn 1's staged project as S1/P3 and built on it.
    assert (
        "  S1 project_created: 'Piano' — Piano 2h a week."
        in fake.requests[2]["messages"][-1]["content"]
    )
    assert [m["kind"] for m in second["staged"]] == ["project_created", "pin_added"]
    statuses = {cs_id: status for cs_id, status, _ in await changesets(maker, who.user_id)}
    assert statuses[uuid.UUID(first["id"])] == "expired"
    assert statuses[uuid.UUID(second["id"])] == "pending"


async def test_discarding_everything_retires_the_pending_changeset(
    agent_app, client, fake, maker, who
):
    fake.script = [
        Call([Tool("stage_exception_add", LAPTOP)], "tool_use"),
        Call([Text("Noted.")]),
        Call([Tool("discard_staged", {"staged": "S1"})], "tool_use"),
        Call([Text("Dropped it.")]),
    ]
    first = (await turn(client, ("user", "No laptop")))[-1]["data"]["changeset"]
    done = (await turn(client, ("user", "Actually never mind")))[-1]["data"]
    assert done["changeset"] is None
    statuses = {cs_id: status for cs_id, status, _ in await changesets(maker, who.user_id)}
    assert statuses[uuid.UUID(first["id"])] == "rejected"


async def test_onboarding_turn_without_preferences(agent_app, client, fake, maker, who):
    who.user_id = await new_user(maker)
    fake.script = [
        Call([Tool("stage_project_create", {
            "name": "Thesis", "kind": "deadline", "target_date": "2026-12-15",
            "estimated_total_hours": 40, "weekly_target_hours": None, "max_block_minutes": 90,
            "min_block_minutes": None, "preferred_windows": None, "preferred_strict": False,
            "requirements": [], "new_tags": ["deep-work"],
            "interpretation": "Thesis, 40h by Dec 15.",
        })], "tool_use"),
        Call([Text("When do you like to work?")]),
    ]  # fmt: skip
    events = await turn(client, ("user", "I need to finish my thesis, about 40 hours, by Dec 15"))
    snapshot = fake.requests[0]["messages"][-1]["content"]
    assert "Preferences: not set yet" in snapshot and "Tags: (none yet)" in snapshot
    cs = events[-1]["data"]["changeset"]
    assert cs["plan"] is None and cs["diff"] is None
    assert cs["staged"][0]["payload"]["project"]["requirements"] == ["deep-work"]
    assert cs["staged"][0]["interpretation"] == "Thesis, 40h by Dec 15. (new tags: deep-work)"
    assert events[-1]["data"]["reply"].endswith(
        "Your schedule appears once your preferences are set."
    )


def test_now_is_inside_the_story_week():
    assert NOW.isoformat() == "2026-11-17T18:30:00+00:00"


# ---------------------------------------------------------------- review fixes (T3.2)


async def test_open_timeout_is_503(agent_app, client, fake):
    agent_app.state.agent_settings = AgentSettings(open_timeout_seconds=0.2)
    fake.script = [Hang(5)]
    res = await client.post(
        "/agent/message", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert res.status_code == 503 and res.json()["error"]["code"] == "agent_unavailable"


def _proposer(maker, uid, pending_id):
    return DbProposer(
        maker, uid, now=NOW, device_tz="America/Los_Angeles", committed_names={},
        pending_id=pending_id, busy=GcalBusy(maker, _no_google),
    )  # fmt: skip


def _no_google() -> Any:
    raise AssertionError("no Google connection: the runtime must not be built")


EXTRA = exception_added(at(3, "09:00"), at(3, "10:00"), uuid.uuid4())


async def test_commit_refuses_when_pending_was_replaced(maker, who):
    base = await propose(maker, who.user_id, [EXTRA], now=NOW, horizon=None)
    newer = exception_added(at(4, "09:00"), at(4, "10:00"), uuid.uuid4())
    await propose(maker, who.user_id, [newer], now=NOW, horizon=None)  # another device
    with pytest.raises(StaleBase):
        await _proposer(maker, who.user_id, base).commit(staged(EXTRA))


async def test_commit_refuses_when_a_pending_appeared(maker, who):
    await propose(maker, who.user_id, [EXTRA], now=NOW, horizon=None)
    with pytest.raises(StaleBase):  # the turn loaded "no pending"
        await _proposer(maker, who.user_id, None).commit(staged(EXTRA))


async def test_reshowing_pending_proposal_returns_its_card(agent_app, client, fake, maker, who):
    base = await propose(maker, who.user_id, [EXTRA], now=NOW, horizon=None)
    fake.script = [Call([Tool("propose_schedule", {})], "tool_use"), Call([Text("Here it is.")])]
    before = await changesets(maker, who.user_id)
    done = (await turn(client, ("user", "Show me the proposal again")))[-1]["data"]
    assert done["changeset"]["id"] == str(base) and done["changeset"]["status"] == "pending"
    assert await changesets(maker, who.user_id) == before  # nothing written


async def test_snapshot_details_pending_entries_and_removals(agent_app, client, fake, maker, who):
    # A manual pending exception has no interpretation: its facts must still be listed.
    fx = exception_added(at(3, "09:00"), at(3, "10:00"), uuid.uuid4())
    fx["source"] = "manual"
    await propose(maker, who.user_id, [fx], now=NOW, horizon=None)
    fake.script = [Call([Text("Ok.")])]
    await turn(client, ("user", "What's pending?"))
    text = fake.requests[0]["messages"][-1]["content"]
    assert "  S1 exception_added: Thu 2026-11-19 09:00–10:00 blocks all work (away)" in text


# ---------------------------------------------------------------- review round 2 (T3.2)


async def test_snapshot_uses_the_pending_preferences_timezone(agent_app, client, fake, maker, who):
    await propose(maker, who.user_id, [prefs(timezone="America/New_York")], now=NOW, horizon=None)
    fake.script = [Call([Text("Ok.")])]
    await turn(client, ("user", "What time is it for you?"))
    assert (
        "Now: Tue 2026-11-17 13:30 (America/New_York)"
        in fake.requests[0]["messages"][-1]["content"]
    )


async def test_inapplicable_pending_is_replaced_not_reported_stale(
    agent_app, client, fake, maker, who
):
    changes = {"max_block_minutes": 30}
    gone = {
        "kind": "project_updated",
        "payload": {"project_id": str(PORTFOLIO), "changes": changes},
    }
    base = await propose(maker, who.user_id, [gone], now=NOW, horizon=None)
    async with maker() as s, s.begin():  # the project vanishes underneath the pending list
        await s.execute(
            text("UPDATE projects SET status = 'archived' WHERE id = :p"), {"p": PORTFOLIO}
        )
    fake.script = [Call([Tool("stage_exception_add", LAPTOP)], "tool_use"), Call([Text("Ok.")])]
    done = (await turn(client, ("user", "No laptop")))[-1]["data"]
    assert done["outcome"] == "completed" and done["changeset"]["status"] == "pending"
    statuses = {cs_id: status for cs_id, status, _ in await changesets(maker, who.user_id)}
    assert statuses[base] == "expired"


async def test_reshow_after_state_moved_reproposes(agent_app, client, fake, maker, who, clock):
    base = await propose(maker, who.user_id, [EXTRA], now=NOW, horizon=None)
    clock.current = NOW + timedelta(hours=26)  # Wed: blocks passed, the solve differs
    fake.script = [Call([Tool("propose_schedule", {})], "tool_use"), Call([Text("Here.")])]
    done = (await turn(client, ("user", "Show me the proposal again")))[-1]["data"]
    assert done["changeset"]["id"] != str(base) and done["changeset"]["status"] == "pending"
    statuses = {cs_id: status for cs_id, status, _ in await changesets(maker, who.user_id)}
    assert statuses[base] == "expired"


# ---------------------------------------------------------------- T4.3b: discard → drain kick


@pytest.mark.parametrize("repended", [0, 2])
async def test_discarding_kicks_the_outbox_only_when_reject_repended(
    maker, who, monkeypatch, repended
):
    # Discarding a pending `google_edit` (reject re-pends blocks) must write our planned
    # times back, like the REST reject route; other discards must not kick.
    import app.agent.router as router_mod

    base = await propose(maker, who.user_id, [EXTRA], now=NOW, horizon=None)

    async def fake_reject(tx, user_id, changeset_id, *, now):
        assert changeset_id == base
        return repended

    monkeypatch.setattr(router_mod, "reject_changeset", fake_reject)
    kicks: list[int] = []
    proposer = DbProposer(
        maker, who.user_id, now=NOW, device_tz="America/Los_Angeles", committed_names={},
        pending_id=base, busy=GcalBusy(maker, _no_google), kick_outbox=lambda: kicks.append(1),
    )  # fmt: skip
    solve = await proposer.commit([])
    assert (solve.changeset, solve.narration) == (None, None)
    assert kicks == ([1] if repended else [])


# ---------------------------------------------------------------- turn trace (agent_turns)


async def agent_turns(maker, uid) -> list[tuple[Any, ...]]:
    return await rows(
        maker,
        "SELECT outcome, error_code, model_calls, input_tokens, output_tokens, transcript_len,"
        " jsonb_array_length(staged), changeset_id, plan_id, user_message, snapshot"
        " FROM agent_turns WHERE user_id = :u ORDER BY created_at",
        u=uid,
    )


async def test_a_turn_is_traced_without_text_by_default(agent_app, client, fake, maker, who):
    fake.script = [
        Call([Tool("stage_exception_add", LAPTOP)], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("I moved things around. OK?")]),
    ]
    events = await turn(client, ("user", "I don't have my laptop today or tomorrow."))
    cs = events[-1]["data"]["changeset"]
    [row] = await agent_turns(maker, who.user_id)
    assert row == (
        "completed", None, 3, 300, 60, 1, 1,
        uuid.UUID(cs["id"]), uuid.UUID(cs["plan"]["id"]), None, None,
    )  # fmt: skip


async def test_capture_flag_stores_message_and_snapshot(agent_app, client, fake, maker, who):
    agent_app.state.agent_settings = AgentSettings(capture_turns=True)
    fake.script = [Call([Text("Sure, what would you like to change?")])]
    await turn(client, ("user", "hi"), ("assistant", "Hello!"), ("user", "Can you help?"))
    [row] = await agent_turns(maker, who.user_id)
    assert row[:9] == ("completed", None, 1, 100, 20, 3, 0, None, None)
    assert row[9] == "Can you help?"
    assert row[10].startswith("Current state (read-only")


async def test_a_refusal_is_traced(agent_app, client, fake, maker, who):
    fake.script = [Call([], "refusal")]
    await turn(client, ("user", "write me a poem"))
    [row] = await agent_turns(maker, who.user_id)
    assert row[:3] == ("refused", None, 1) and row[7:] == (None, None, None, None)


async def test_an_upstream_outage_before_the_stream_is_traced(agent_app, client, fake, maker, who):
    fake.script = [Failure(529)]
    res = await client.post(
        "/agent/message", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert res.status_code == 503
    [row] = await agent_turns(maker, who.user_id)
    assert row[:3] == ("error", "agent_unavailable", 1)

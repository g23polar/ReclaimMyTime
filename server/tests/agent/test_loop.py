"""T3.2 turn loop over a mocked Anthropic transport (no DB: a fake Proposer)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic
import pytest

from app.agent.loop import DECLINE, FALLBACK, STALE, AgentTurn, Committed, StaleBase
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.schemas import tool_definitions
from app.agent.settings import AgentSettings
from app.agent.staging import ToolError
from tests.agent.fake_anthropic import Call, Failure, FakeAnthropic, Hang, Text, Tool
from tests.agent.test_staging import _create, _turn

CHANGESET = {"id": "cs-1"}  # opaque to the loop


@dataclass
class Msg:
    role: str
    content: str


class FakeProposer:
    """Fingerprints a solve by its staged list unless `drift` says committed state moved."""

    def __init__(self, narration: str = "Add Novel: 5h every week.", *, drift=False) -> None:
        self.narration = narration
        self.drift = drift
        self.dry_runs: list[list[Any]] = []
        self.commits: list[list[Any]] = []
        self.current_calls = 0

    async def dry_run(self, staged):
        if not staged:
            raise ToolError("nothing is staged, so there is nothing to propose")
        self.dry_runs.append(list(staged))
        return f"Proposed schedule (fits): {self.narration}"

    async def commit(self, staged):
        self.commits.append(list(staged))
        if not staged:
            return Committed(None, None, matches_dry_run=True)
        seen = bool(self.dry_runs) and self.dry_runs[-1] == list(staged) and not self.drift
        return Committed(CHANGESET, self.narration, matches_dry_run=seen)

    async def reshow(self, staged):
        self.current_calls += 1
        return {"id": "pending-cs"}


async def run(
    fake: FakeAnthropic, *, staging=None, proposer=None, calls=8, transcript=None, deadline=120.0
):
    staging = staging or _turn()
    proposer = proposer or FakeProposer()
    turn = AgentTurn(
        client=fake.client(),
        settings=AgentSettings(max_model_calls=calls, turn_timeout_seconds=deadline),
        transcript=transcript or [Msg("user", "I want to write my novel 5h a week")],
        staging=staging,
        snapshot="STATE",
        proposer=proposer,
    )
    await turn.open()
    events = [e async for e in turn.events()]
    return events, proposer, staging


def names(events):
    return [name for name, _ in events]


def terminal(events):
    assert names(events).count("done") + names(events).count("error") == 1
    assert events[-1][0] in ("done", "error")
    return events[-1]


# ---------------------------------------------------------------- happy path


async def test_stage_propose_narrate():
    fake = FakeAnthropic(
        Call([Text("Got it."), Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Novel gets Mon and Wed mornings. OK?")]),
    )
    events, proposer, staging = await run(fake)
    assert names(events) == ["delta", "status", "status", "delta", "done"]
    assert events[1] == ("status", {"phase": "staging"})
    assert events[2] == ("status", {"phase": "solving"})
    done = terminal(events)[1]
    assert done == {
        "outcome": "completed",
        "reply": "Got it. Novel gets Mon and Wed mornings. OK?",
        "changeset": CHANGESET,
    }
    # Deltas concatenate to the reply (the client replaces them with done.reply anyway).
    assert "".join(d["text"] for n, d in events if n == "delta") == done["reply"]
    assert len(proposer.dry_runs) == 1 and proposer.commits == [staging.mutations]


async def test_request_shape_cache_order_and_append_only_history():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Text("Staged.")]),
    )
    await run(fake)
    first, second = fake.requests
    assert first["model"] == "claude-haiku-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert "tool_choice" not in first  # forced tool use is a 400 on current models
    assert not {"temperature", "top_p", "top_k"} & first.keys()
    assert first["tools"] == tool_definitions()
    assert first["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert first["messages"] == [
        {"role": "user", "content": "I want to write my novel 5h a week"},
        {"role": "system", "content": "STATE"},
    ]
    # The second request extends the first byte-for-byte: tools/system unchanged, the
    # snapshot stays where it was, the assistant turn (thinking included) is echoed.
    assert second["tools"] == first["tools"] and second["system"] == first["system"]
    assert second["messages"][:2] == first["messages"]
    assistant, results = second["messages"][2:]
    assert assistant["role"] == "assistant"
    assert [b["type"] for b in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig1"
    assert results["role"] == "user"
    assert results["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1_0",
            "content": "Staged S1: new project P3 'Novel'.",
        }
    ]


async def test_parallel_tool_calls_answered_in_one_user_message():
    fake = FakeAnthropic(
        Call(
            [
                Tool("stage_project_create", _create()),
                Tool("stage_project_create", _create(name="Piano")),
                Tool("stage_project_archive", {"project": "P9", "interpretation": "x"}),
            ],
            "tool_use",
        ),
        Call([Text("Done.")]),
    )
    events, _, staging = await run(fake)
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1_0", "toolu_1_1", "toolu_1_2"]
    assert results[2]["is_error"] is True and "unknown project label 'P9'" in results[2]["content"]
    assert names(events).count("status") == 1  # one "staging" per batch
    assert len(staging.staged) == 2


# ---------------------------------------------------------------- auto-propose


async def test_staged_but_not_proposed_is_proposed_with_template_narration():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Text("Added your novel.")]),
    )
    events, proposer, _ = await run(fake)
    done = terminal(events)[1]
    assert done["reply"] == "Added your novel.\n\nAdd Novel: 5h every week."
    assert done["changeset"] == CHANGESET
    assert proposer.dry_runs == [] and len(proposer.commits) == 1


async def test_restaging_after_propose_appends_narration_for_the_final_state():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Tool("stage_project_create", _create(name="Piano"))], "tool_use"),
        Call([Text("Also added piano.")]),
    )
    events, proposer, _ = await run(fake)
    assert terminal(events)[1]["reply"].endswith("\n\nAdd Novel: 5h every week.")
    assert len(proposer.commits[0]) == 2


async def test_propose_with_nothing_staged_is_a_tool_error():
    fake = FakeAnthropic(
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Nothing to change yet. What are you working on?")]),
    )
    events, proposer, _ = await run(fake)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert terminal(events)[1]["changeset"] is None and proposer.commits == []


# ---------------------------------------------------------------- refusal + text-only


async def test_refusal_declines_with_zero_state_change():
    fake = FakeAnthropic(Call([Text("I can")], "refusal"))
    events, proposer, _ = await run(fake)
    assert terminal(events) == (
        "done",
        {"outcome": "refused", "reply": DECLINE, "changeset": None},
    )
    assert proposer.commits == [] and proposer.dry_runs == []


async def test_refusal_after_staging_discards_the_turn():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([], "refusal"),
    )
    events, proposer, _ = await run(fake)
    assert terminal(events)[1]["outcome"] == "refused"
    assert proposer.commits == []


async def test_question_only_turn_has_no_changeset():
    fake = FakeAnthropic(Call([Text("You have 3h of Thesis left this week.")]))
    events, proposer, _ = await run(fake)
    done = terminal(events)[1]
    assert done["changeset"] is None and proposer.commits == []


async def test_empty_reply_falls_back():
    fake = FakeAnthropic(Call([]))
    events, _, _ = await run(fake)
    assert terminal(events)[1]["reply"] == FALLBACK


async def test_max_tokens_never_runs_a_truncated_tool_call():
    fake = FakeAnthropic(Call([Tool("stage_project_create", _create())], "max_tokens"))
    events, proposer, staging = await run(fake)
    assert staging.staged == [] and proposer.commits == []
    assert terminal(events)[0] == "done"


# ---------------------------------------------------------------- cap


async def test_cap_with_staged_changes_ends_like_a_turn():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("stage_project_archive", {"project": "P9", "interpretation": "x"})], "tool_use"),
    )
    events, proposer, _ = await run(fake, calls=2)
    done = terminal(events)
    assert done[0] == "done" and done[1]["changeset"] == CHANGESET
    assert len(fake.requests) == 2 and len(proposer.commits) == 1


async def test_cap_without_staged_changes_is_an_error():
    loop = Call(
        [Tool("stage_project_archive", {"project": "P9", "interpretation": "x"})], "tool_use"
    )
    fake = FakeAnthropic(loop, loop, loop)
    events, proposer, _ = await run(fake, calls=3)
    assert terminal(events)[1]["error"]["code"] == "agent_unavailable"
    assert proposer.commits == []


# ---------------------------------------------------------------- upstream failures


async def test_open_failure_raises_before_any_event():
    fake = FakeAnthropic(Failure(529))
    turn = AgentTurn(
        client=fake.client(),
        settings=AgentSettings(),
        transcript=[Msg("user", "hi")],
        staging=_turn(),
        snapshot="STATE",
        proposer=FakeProposer(),
    )
    with pytest.raises(anthropic.APIStatusError):
        await turn.open()


async def test_mid_turn_failure_is_a_terminal_error_and_nothing_is_staged():
    fake = FakeAnthropic(
        Call([Text("Got it: no laptop"), Tool("stage_project_create", _create())], "tool_use"),
        Failure(500),
    )
    events, proposer, _ = await run(fake)
    assert names(events)[0] == "delta"
    assert terminal(events) == (
        "error",
        {
            "error": {
                "code": "agent_unavailable",
                "message": "The assistant is temporarily unavailable. Nothing was changed; "
                "try again.",
            }
        },
    )
    assert proposer.commits == []


async def test_commit_failure_is_an_internal_error():
    class Broken(FakeProposer):
        async def commit(self, staged):
            raise RuntimeError("db down")

    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"), Call([Text("Ok.")])
    )
    events, _, _ = await run(fake, proposer=Broken())
    assert terminal(events)[1]["error"]["code"] == "internal_error"


# ---------------------------------------------------------------- review fixes (T3.2)


async def test_committed_solve_differing_from_dry_run_is_narrated():
    # Review: state moved between dry run and commit → the reply must describe the commit.
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Novel fits Mon and Wed.")]),
    )
    events, _, _ = await run(fake, proposer=FakeProposer(drift=True))
    assert terminal(events)[1]["reply"] == "Novel fits Mon and Wed.\n\nAdd Novel: 5h every week."


async def test_reshowing_unchanged_pending_returns_its_card_without_writing():
    first = _turn()
    first.call("stage_project_create", _create())
    fake = FakeAnthropic(
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Here it is again. OK?")]),
    )
    proposer = FakeProposer()
    events, _, _ = await run(fake, staging=_turn(pending=first.mutations), proposer=proposer)
    assert terminal(events)[1]["changeset"] == {"id": "pending-cs"}
    assert proposer.commits == [] and proposer.current_calls == 1


async def test_stale_base_is_a_terminal_error():
    class Stale(FakeProposer):
        async def commit(self, staged):
            raise StaleBase()

    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"), Call([Text("Ok.")])
    )
    events, _, _ = await run(fake, proposer=Stale())
    assert terminal(events) == ("error", {"error": {"code": "changeset_stale", "message": STALE}})


async def test_nul_in_tool_input_is_a_tool_error():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create(name="No\u0000vel"))], "tool_use"),
        Call([Text("Hmm.")]),
    )
    events, proposer, staging = await run(fake)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "NUL" in result["content"]
    assert staging.staged == [] and proposer.commits == []


# ---------------------------------------------------------------- review round 2 (T3.2)


async def test_turn_deadline_bounds_later_model_calls():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Hang(5),
    )
    events, proposer, _ = await run(fake, deadline=0.3)
    assert terminal(events)[1]["error"]["code"] == "agent_unavailable"
    assert proposer.commits == []  # nothing staged on a timed-out turn


async def test_closing_the_event_stream_cancels_the_turn():
    fake = FakeAnthropic(Call([Text("Working on it")], "tool_use"), Hang(5))
    turn = AgentTurn(
        client=fake.client(),
        settings=AgentSettings(),
        transcript=[Msg("user", "hi")],
        staging=_turn(),
        snapshot="STATE",
        proposer=FakeProposer(),
    )
    await turn.open()
    events = turn.events()
    assert (await anext(events))[0] == "delta"
    await events.aclose()  # returns promptly instead of waiting out the 5 s upstream hang


def test_redacted_trace_has_no_exception_text():
    from app.agent.loop import _redacted_trace

    private = "Therapy " + "notes"  # built at runtime: never appears in a source line
    try:
        try:
            raise ValueError(private)
        except ValueError as inner:
            raise RuntimeError(f"insert failed: {private}") from inner
    except RuntimeError as exc:
        trace = _redacted_trace(exc)
    assert "builtins.RuntimeError" in trace and "builtins.ValueError" in trace
    assert "test_redacted_trace_has_no_exception_text" in trace  # frames kept
    assert private not in trace


async def test_unpaired_surrogate_is_a_tool_error():
    staging = _turn()
    with pytest.raises(ToolError, match="valid UTF-8"):
        staging.call("stage_project_create", _create(name="Nov\ud800el"))

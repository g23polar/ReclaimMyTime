"""T3.2 turn loop over a mocked Anthropic transport (no DB: a fake Proposer)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic
import pytest

from app.agent.loop import (
    DECLINE,
    FALLBACK,
    STALE,
    UPDATED,
    AgentTurn,
    DryRun,
    Solve,
    StaleBase,
    drain_traces,
    upstream_summary,
)
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
    """Fingerprints a solve by its staged list; `drift` makes the committed solve differ
    from every dry run (state moved underneath the turn)."""

    def __init__(self, narration: str = "Add Novel: 5h every week.", *, drift=False) -> None:
        self.narration = narration
        self.drift = drift
        self.dry_runs: list[list[Any]] = []
        self.commits: list[list[Any]] = []
        self.reshows: list[str | None] = []

    @staticmethod
    def fp(staged) -> str:
        return repr(list(staged))

    async def dry_run(self, staged):
        if not staged:
            raise ToolError("nothing is staged, so there is nothing to propose")
        self.dry_runs.append(list(staged))
        return DryRun(f"Proposed schedule (fits): {self.narration}", self.fp(staged))

    async def commit(self, staged):
        self.commits.append(list(staged))
        if not staged:
            return Solve(None, None, None)
        return Solve(CHANGESET, self.narration, self.fp(staged) + ("~" if self.drift else ""))

    async def reshow(self, staged, expected):
        self.reshows.append(expected)
        fp = self.fp(staged) + ("~" if self.drift else "")
        return Solve({"id": "pending-cs"}, self.narration, fp)


async def run(
    fake: FakeAnthropic,
    *,
    staging=None,
    proposer=None,
    calls=8,
    transcript=None,
    deadline=120.0,
    recorder=None,
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
        recorder=recorder,
    )
    await turn.open()
    events = [e async for e in turn.events()]
    await drain_traces()
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
    # The model never saw a solve: its text is replaced by the template (M3 gate).
    assert done["reply"] == f"{UPDATED} Add Novel: 5h every week."
    assert done["changeset"] == CHANGESET
    assert proposer.dry_runs == [] and len(proposer.commits) == 1


async def test_restaging_after_propose_replaces_reply_with_the_final_solve():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Tool("stage_project_create", _create(name="Piano"))], "tool_use"),
        Call([Text("Also added piano.")]),
    )
    events, proposer, _ = await run(fake)
    assert terminal(events)[1]["reply"] == f"{UPDATED} Add Novel: 5h every week."
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
    assert done[1]["reply"] == f"{UPDATED} Add Novel: 5h every week."
    assert len(fake.requests) == 2 and len(proposer.commits) == 1


async def test_propose_on_the_last_allowed_call_is_explained_by_the_template():
    # M3 gate: the model never received this dry run's result, so it never explained it —
    # even though the committed solve matches it.
    fake = FakeAnthropic(
        Call([Text("All set, everything fits!"), Tool("stage_project_create", _create())],
             "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
    )  # fmt: skip
    events, proposer, _ = await run(fake, calls=2)
    done = terminal(events)[1]
    assert done["reply"] == f"{UPDATED} Add Novel: 5h every week."
    assert len(proposer.dry_runs) == 1 and len(proposer.commits) == 1


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


async def test_committed_solve_differing_from_dry_run_replaces_the_reply():
    # State moved between dry run and commit: the model's text describes another schedule.
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Novel fits Mon and Wed.")]),
    )
    events, _, _ = await run(fake, proposer=FakeProposer(drift=True))
    assert terminal(events)[1]["reply"] == f"{UPDATED} Add Novel: 5h every week."


async def test_reshowing_unchanged_pending_returns_its_card_without_writing():
    first = _turn()
    first.call("stage_project_create", _create())
    fake = FakeAnthropic(
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Here it is again. OK?")]),
    )
    proposer = FakeProposer()
    events, _, _ = await run(fake, staging=_turn(pending=first.mutations), proposer=proposer)
    done = terminal(events)[1]
    assert done["changeset"] == {"id": "pending-cs"}
    assert done["reply"] == "Here it is again. OK?"  # the model narrated this very solve
    assert proposer.commits == [] and proposer.reshows == [FakeProposer.fp(first.mutations)]


async def test_reshow_that_drifted_replaces_the_reply():
    # M3 gate: re-show re-committed a solve that differs from the narrated dry run.
    first = _turn()
    first.call("stage_project_create", _create())
    fake = FakeAnthropic(
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Same as before, all fits.")]),
    )
    proposer = FakeProposer(drift=True)
    events, _, _ = await run(fake, staging=_turn(pending=first.mutations), proposer=proposer)
    assert terminal(events)[1]["reply"] == f"{UPDATED} Add Novel: 5h every week."


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


# ---------------------------------------------------------------- upstream log summary


def test_upstream_summary_has_status_class_and_request_id_but_no_message():
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    response = httpx.Response(400, request=request, headers={"request-id": "req_123"})
    exc = anthropic.BadRequestError("secret user text", response=response, body=None)

    summary = upstream_summary(exc)

    assert summary == "status=400 type=BadRequestError request_id=req_123"
    assert "secret user text" not in summary


def test_upstream_summary_handles_errors_without_a_response():
    import anthropic
    import httpx

    exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.test"))

    assert upstream_summary(exc) == "status=None type=APIConnectionError request_id=None"


# ---------------------------------------------------------------- turn trace (eval capture)


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.summaries: list[Any] = []

    async def __call__(self, summary) -> None:
        self.summaries.append(summary)
        if self.fail:
            raise RuntimeError("db down")


async def test_trace_sums_usage_and_records_the_committed_changeset():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([Tool("propose_schedule", {})], "tool_use"),
        Call([Text("Novel gets Mon and Wed mornings. OK?")]),
    )
    rec = Recorder()
    events, _, staging = await run(fake, recorder=rec)
    assert terminal(events)[1]["outcome"] == "completed"
    [s] = rec.summaries
    assert (s.outcome, s.error_code, s.stop_reason, s.model_calls) == (
        "completed",
        None,
        "end_turn",
        3,
    )
    # The fake reports 100 input / 20 output tokens per call, no cache fields.
    assert (s.input_tokens, s.output_tokens, s.cache_read_tokens, s.cache_write_tokens) == (
        300,
        60,
        0,
        0,
    )
    assert s.changeset == CHANGESET and list(s.staged) == staging.mutations


async def test_trace_records_a_refusal():
    rec = Recorder()
    await run(FakeAnthropic(Call([Text("I can")], "refusal")), recorder=rec)
    [s] = rec.summaries
    assert (s.outcome, s.stop_reason, s.model_calls, s.changeset) == ("refused", "refusal", 1, None)


async def test_trace_records_the_error_code():
    loop = Call(
        [Tool("stage_project_archive", {"project": "P9", "interpretation": "x"})], "tool_use"
    )
    rec = Recorder()
    await run(FakeAnthropic(loop, loop, loop), calls=3, recorder=rec)
    [s] = rec.summaries
    assert (s.outcome, s.error_code, s.model_calls) == ("error", "agent_unavailable", 3)


async def test_a_failing_trace_never_reaches_the_user():
    fake = FakeAnthropic(Call([Text("Sure.")]))
    rec = Recorder(fail=True)
    events, _, _ = await run(fake, recorder=rec)
    assert terminal(events) == (
        "done",
        {"outcome": "completed", "reply": "Sure.", "changeset": None},
    )
    assert len(rec.summaries) == 1


async def test_trace_of_a_refused_turn_keeps_nothing_staged():
    fake = FakeAnthropic(
        Call([Tool("stage_project_create", _create())], "tool_use"),
        Call([], "refusal"),
    )
    rec = Recorder()
    await run(fake, recorder=rec)
    [s] = rec.summaries
    assert (s.outcome, s.model_calls, tuple(s.staged)) == ("refused", 2, ())


async def test_a_disconnect_is_traced_as_cancelled():
    fake = FakeAnthropic(
        Call([Text("Working on it"), Tool("stage_project_create", _create())], "tool_use"),
        Hang(5),
    )
    rec = Recorder()
    turn = AgentTurn(
        client=fake.client(),
        settings=AgentSettings(),
        transcript=[Msg("user", "hi")],
        staging=_turn(),
        snapshot="STATE",
        proposer=FakeProposer(),
        recorder=rec,
    )
    await turn.open()
    events = turn.events()
    assert (await anext(events))[0] == "delta"
    await events.aclose()
    await drain_traces()
    [s] = rec.summaries
    # The hanging second call was opened, so it counts; nothing was committed.
    assert (s.outcome, s.error_code, s.model_calls, tuple(s.staged)) == (
        "error",
        "cancelled",
        2,
        (),
    )


async def test_trace_survives_the_client_closing_right_after_done():
    # The app closes the stream as soon as it reads `done` (prod, 2026-11-18): the trace
    # write must not run inside the response task, or that close cancels it mid-write.
    import asyncio

    written = []

    async def slow_recorder(summary) -> None:
        await asyncio.sleep(0.05)
        written.append(summary)

    turn = AgentTurn(
        client=FakeAnthropic(Call([Text("Sure.")])).client(),
        settings=AgentSettings(),
        transcript=[Msg("user", "hi")],
        staging=_turn(),
        snapshot="STATE",
        proposer=FakeProposer(),
        recorder=slow_recorder,
    )
    await turn.open()
    events = turn.events()
    async for name, _ in events:
        if name == "done":
            break
    await events.aclose()
    await drain_traces()
    [s] = written
    assert (s.outcome, s.error_code) == ("completed", None)

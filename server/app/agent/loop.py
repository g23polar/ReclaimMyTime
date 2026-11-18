"""One agent turn: the manual tool-use loop behind `POST /agent/message` (ARCH §8, T3.2).

Not the SDK tool runner: between model calls we emit `status` events, validate every tool
input against the Pydantic drafts, and stop on refusal without touching state.

Write discipline: tool calls stage in memory (`TurnStaging`); `propose_schedule` solves on a
rolled-back transaction (`Proposer.dry_run`) so the model can narrate the result; the one
real write is `Proposer.commit`, right before `done`. An `error` therefore never leaves
anything staged (contract), and no DB lock is held while the model is generating.

The conversation is append-only within the turn (tools, system prompt and the state
snapshot are fixed when the turn starts) — Haiku 5.5 rejects replayed thinking blocks after
any edit to what preceded them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from anthropic.lib.streaming import AsyncMessageStream
from pydantic import BaseModel

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.schemas import tool_definitions
from app.agent.settings import AgentSettings
from app.agent.staging import ToolError, TurnStaging
from app.db.models.staged import StagedMutation

log = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]

DECLINE = (
    "Sorry, I can't help with that. I can help with your week: projects, availability, "
    "or moving blocks."
)
FALLBACK = "Sorry, I didn't catch that. Could you say it another way?"
UPDATED = "Here's the proposal:"
UNAVAILABLE = "The assistant is temporarily unavailable. Nothing was changed; try again."
INTERNAL = "Something went wrong on our side. Nothing was changed; try again."
STALE = (
    "Your pending changes were updated somewhere else while I was working. Nothing was "
    "changed; try again."
)

_TOOLS = tool_definitions()
_SYSTEM = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


class TranscriptMessage(Protocol):
    role: str
    content: str


class TurnAborted(Exception):
    """Ends the turn with an in-stream `error` event carrying `code` (T4.2: busy time could
    not be read — `gcal_unavailable` / `gcal_reconnect_required`). Nothing was committed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class StaleBase(Exception):
    """The pending changeset this turn built on was confirmed, rejected or replaced while
    the model was generating; committing would overwrite or resurrect it."""


@dataclass(frozen=True)
class DryRun:
    text: str  # the model's tool result
    fingerprint: str  # identifies the solve (see Solve.fingerprint)


@dataclass(frozen=True)
class Solve:
    changeset: dict[str, Any] | None
    narration: str | None  # template text for this solve
    fingerprint: str | None  # what the user is shown, minus ids; None when no changeset


@dataclass
class TurnSummary:
    """What a finished turn did, for the agent_turns trace (eval capture, token budget).
    Filled as the turn runs; handed to the recorder once, after the terminal event."""

    outcome: str = "error"  # "completed" | "refused" | "error"
    error_code: str | None = None
    stop_reason: str | None = None  # of the last model call
    model_calls: int = 0  # streams opened; a call that fails mid-stream counts, its tokens don't
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    staged: Sequence[StagedMutation] = ()  # completed turns only: others change nothing
    changeset: dict[str, Any] | None = None


Recorder = Callable[[TurnSummary], Awaitable[None]]
_RECORD_TIMEOUT = 5.0  # seconds; a stuck trace write is abandoned, never retried

# Trace writes run detached from the request: the app closes the SSE stream as soon as it
# reads `done`, which cancels the response task, and a write inside it would be cut off
# mid-transaction (seen on prod 2026-11-18). Strong refs keep the tasks alive until done.
_TRACES: set[asyncio.Task[None]] = set()


async def drain_traces() -> None:
    """Wait for pending trace writes (tests, shutdown)."""
    while pending := [t for t in _TRACES if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


class Proposer(Protocol):
    async def dry_run(self, staged: Sequence[StagedMutation]) -> DryRun:
        """Solve on a rolled-back transaction. Raises ToolError when the staged list cannot
        be proposed."""

    async def commit(self, staged: Sequence[StagedMutation]) -> Solve:
        """Persist the proposal (or, for an emptied list, retire the pending changeset).
        Raises StaleBase if the pending base changed during the turn."""

    async def reshow(self, staged: Sequence[StagedMutation], expected: str | None) -> Solve:
        """The still-pending changeset this turn built on, unchanged — unless its stored
        solve differs from `expected` (the latest dry run), in which case that solve is
        committed instead. Raises StaleBase like commit."""


class AgentTurn:
    def __init__(
        self,
        *,
        client: anthropic.AsyncAnthropic,
        settings: AgentSettings,
        transcript: Sequence[TranscriptMessage],
        staging: TurnStaging,
        snapshot: str,
        proposer: Proposer,
        recorder: Recorder | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._staging = staging
        self._proposer = proposer
        self._recorder = recorder
        self.summary = TurnSummary()
        self._terminal = False  # a done/error event was emitted
        # The snapshot follows the last user message: a mid-conversation system message must
        # come after a user turn and before the assistant turn (cache-safe, no beta).
        self._messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in transcript
        ]
        self._messages.append({"role": "system", "content": snapshot})
        self._stack = AsyncExitStack()
        self._first: AsyncMessageStream | None = None

    async def open(self) -> None:
        """Start the first model call. Called before the HTTP 200 so an upstream outage is a
        plain 503 (contract); raises anthropic.APIError."""
        self.summary.model_calls += 1
        try:
            self._first = await self._stack.enter_async_context(self._stream())
        except BaseException:
            await self._stack.aclose()
            raise

    async def record_failed_open(self) -> None:
        """Trace a turn whose first model call failed before the 200 (the caller's 503)."""
        self.summary.error_code = "agent_unavailable"
        self._terminal = True
        self._record()

    async def events(self) -> AsyncIterator[Event]:
        """The turn runs in its own task under one deadline (`turn_timeout_seconds`), so an
        upstream that keeps a stream open without finishing cannot hold the request and its
        pooled connection forever. Closing this iterator (client gone) cancels the task."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        task = asyncio.create_task(self._produce(queue))
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _produce(self, queue: asyncio.Queue[Event | None]) -> None:
        emit = self._emitter(queue)
        try:
            async with self._stack, asyncio.timeout(self._settings.turn_timeout_seconds):
                async for event in self._run():
                    await emit(event)
        except TimeoutError:
            log.warning("agent turn exceeded %ss", self._settings.turn_timeout_seconds)
            await emit(_error("agent_unavailable", UNAVAILABLE))
        except anthropic.APIError as exc:
            log.warning("agent upstream failure: %s", upstream_summary(exc))
            await emit(_error("agent_unavailable", UNAVAILABLE))
        except StaleBase:
            await emit(_error("changeset_stale", STALE))
        except TurnAborted as exc:
            await emit(_error(exc.code, exc.message))
        except Exception as exc:
            # Exception text (driver errors included) can quote user data: log types and
            # frames only. The engine also hides SQL parameters (session.py).
            log.error("agent turn failed:\n%s", _redacted_trace(exc))
            await emit(_error("internal_error", INTERNAL))
        finally:
            self._record()
            await queue.put(None)

    def _emitter(self, queue: asyncio.Queue[Event | None]) -> Callable[[Event], Awaitable[None]]:
        """Queue an event, noting the terminal one in the summary."""

        async def emit(event: Event) -> None:
            name, data = event
            self._terminal = self._terminal or name in ("done", "error")
            if name == "done":
                self.summary.outcome = data["outcome"]
                self.summary.changeset = data["changeset"]
            elif name == "error":
                self.summary.outcome = "error"
                self.summary.error_code = data["error"]["code"]
            await queue.put(event)

        return emit

    def _record(self) -> None:
        """Schedule the trace write (synchronous, so it also works while this task is being
        cancelled). Best-effort: failures are logged, never surfaced to the user."""
        if self._recorder is None:
            return
        s = self.summary
        if not self._terminal:
            s.error_code = "cancelled"  # the client went away before the turn ended
        s.staged = tuple(self._staging.mutations) if s.outcome == "completed" else ()
        task = asyncio.create_task(_write_trace(self._recorder, s))
        _TRACES.add(task)
        task.add_done_callback(_TRACES.discard)

    # ------------------------------------------------------------ loop

    def _stream(self) -> Any:
        return self._client.messages.stream(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=self._messages,
            thinking={"type": "adaptive"},
        )

    async def _run(self) -> AsyncIterator[Event]:
        staging = self._staging
        text: list[str] = []
        latest: str | None = None  # fingerprint of the latest successful dry run
        narrated: str | None = None  # …of the latest dry run a later model call received
        finished = False
        for call in range(self._settings.max_model_calls):
            narrated = latest  # this call sees every tool result returned so far
            if call == 0:
                assert self._first is not None, "open() first"
                stream = self._first
            else:
                self.summary.model_calls += 1
                stream = await self._stack.enter_async_context(self._stream())
            first_text = True
            async for event in stream:
                if event.type == "text" and event.text:
                    chunk = event.text
                    if (
                        first_text
                        and text
                        and not text[-1][-1:].isspace()
                        and not chunk[:1].isspace()
                    ):
                        chunk = " " + chunk  # separate this call's text from the last call's
                    first_text = False
                    text.append(chunk)
                    yield "delta", {"text": chunk}
            message = await stream.get_final_message()
            self._count(message)

            if message.stop_reason == "refusal":
                # Haiku has no server-side fallback: decline, zero state change.
                yield "done", {"outcome": "refused", "reply": DECLINE, "changeset": None}
                return
            tool_uses = [b for b in message.content if b.type == "tool_use"]
            self._messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason != "tool_use" or not tool_uses:
                finished = True  # end_turn, or max_tokens (a truncated tool call never runs)
                break

            results: list[dict[str, Any]] = []
            announced_staging = False
            for block in tool_uses:
                if block.name == "propose_schedule":
                    yield "status", {"phase": "solving"}
                    try:
                        dry = await self._proposer.dry_run(staging.mutations)
                        latest = dry.fingerprint
                        results.append(_result(block.id, dry.text))
                    except ToolError as exc:
                        results.append(_result(block.id, str(exc), error=True))
                    continue
                if not announced_staging:
                    yield "status", {"phase": "staging"}
                    announced_staging = True
                try:
                    results.append(_result(block.id, staging.call(block.name, _input(block))))
                except ToolError as exc:
                    results.append(_result(block.id, str(exc), error=True))
            # All results of one assistant turn go back in a single user message.
            self._messages.append({"role": "user", "content": results})

        if not finished:
            log.warning("agent turn hit the %d-call cap", self._settings.max_model_calls)
            if not staging.changed:
                yield _error("agent_unavailable", UNAVAILABLE)
                return

        solve: Solve | None = None
        if staging.changed:
            solve = await self._proposer.commit(staging.mutations)
        elif latest is not None:
            # Re-showing the unchanged pending proposal (re-committed only if it drifted).
            solve = await self._proposer.reshow(staging.mutations, latest)
        reply = "".join(text).strip()
        if solve is not None and solve.changeset is not None and solve.fingerprint != narrated:
            # The model never explained the persisted solve: it staged without proposing,
            # its last call ended on the propose, or state moved after its dry run. Its text
            # may describe another schedule, so the template replaces it (M3 gate, user
            # decision 2026-11-15: done.reply must describe done.changeset). Clients replace
            # streamed deltas with done.reply.
            reply = f"{UPDATED} {solve.narration}"
        changeset = None if solve is None else solve.changeset
        yield "done", {"outcome": "completed", "reply": reply or FALLBACK, "changeset": changeset}

    def _count(self, message: Any) -> None:
        s, usage = self.summary, message.usage
        s.stop_reason = message.stop_reason
        s.input_tokens += usage.input_tokens or 0
        s.output_tokens += usage.output_tokens or 0
        s.cache_read_tokens += getattr(usage, "cache_read_input_tokens", None) or 0
        s.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", None) or 0


async def _write_trace(recorder: Recorder, summary: TurnSummary) -> None:
    try:
        async with asyncio.timeout(_RECORD_TIMEOUT):
            await recorder(summary)
    except Exception as exc:
        log.warning("agent turn trace not recorded:\n%s", _redacted_trace(exc))


def _redacted_trace(exc: BaseException) -> str:
    """Exception types and stack frames of the whole chain, without any message text."""
    out: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        out.append(f"{type(current).__module__}.{type(current).__qualname__}")
        out.extend(traceback.format_tb(current.__traceback__))
        current = current.__cause__ or current.__context__
    return "".join(line if line.endswith("\n") else line + "\n" for line in out)


def _input(block: Any) -> dict[str, Any]:
    raw = block.input
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    return dict(raw) if isinstance(raw, dict) else {}


def _result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        out["is_error"] = True
    return out


def _error(code: str, message: str) -> Event:
    return "error", {"error": {"code": code, "message": message}}


def upstream_summary(exc: BaseException) -> str:
    """Status, class and request id of an Anthropic failure, for logs only.

    Never the message: API error text can echo request content, and the request id is
    what Anthropic support needs to find the call."""
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    return f"status={status} type={type(exc).__name__} request_id={request_id}"

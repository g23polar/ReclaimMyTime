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
from collections.abc import AsyncIterator, Sequence
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


class StaleBase(Exception):
    """The pending changeset this turn built on was confirmed, rejected or replaced while
    the model was generating; committing would overwrite or resurrect it."""


@dataclass(frozen=True)
class Committed:
    changeset: dict[str, Any] | None
    narration: str | None  # template text for the persisted solve
    matches_dry_run: bool  # the persisted solve is the one the model last saw and narrated


class Proposer(Protocol):
    async def dry_run(self, staged: Sequence[StagedMutation]) -> str:
        """Solve on a rolled-back transaction; text for the model's tool result.
        Raises ToolError when the staged list cannot be proposed."""

    async def commit(self, staged: Sequence[StagedMutation]) -> Committed:
        """Persist the proposal (or, for an emptied list, retire the pending changeset).
        Raises StaleBase if the pending base changed during the turn."""

    async def reshow(self, staged: Sequence[StagedMutation]) -> dict[str, Any] | None:
        """The still-pending changeset this turn built on, unchanged. If its stored solve
        differs from the last dry run (time passed, state moved), the dry-run solve is
        committed instead so the card matches the narration. Raises StaleBase like commit."""


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
    ) -> None:
        self._client = client
        self._settings = settings
        self._staging = staging
        self._proposer = proposer
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
        try:
            self._first = await self._stack.enter_async_context(self._stream())
        except BaseException:
            await self._stack.aclose()
            raise

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
        try:
            async with self._stack, asyncio.timeout(self._settings.turn_timeout_seconds):
                async for event in self._run():
                    await queue.put(event)
        except TimeoutError:
            log.warning("agent turn exceeded %ss", self._settings.turn_timeout_seconds)
            await queue.put(_error("agent_unavailable", UNAVAILABLE))
        except anthropic.APIError as exc:
            log.warning("agent upstream failure: %s", type(exc).__name__)
            await queue.put(_error("agent_unavailable", UNAVAILABLE))
        except StaleBase:
            await queue.put(_error("changeset_stale", STALE))
        except Exception as exc:
            # Exception text (driver errors included) can quote user data: log types and
            # frames only. The engine also hides SQL parameters (session.py).
            log.error("agent turn failed:\n%s", _redacted_trace(exc))
            await queue.put(_error("internal_error", INTERNAL))
        finally:
            await queue.put(None)

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
        proposed = False  # propose_schedule ran (successfully) at least once
        finished = False
        for call in range(self._settings.max_model_calls):
            if call == 0:
                assert self._first is not None, "open() first"
                stream = self._first
            else:
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
                        content = await self._proposer.dry_run(staging.mutations)
                        proposed = True
                        results.append(_result(block.id, content))
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

        changeset = narration = None
        if staging.changed:
            committed = await self._proposer.commit(staging.mutations)
            changeset = committed.changeset
            if not committed.matches_dry_run:
                # The model never saw this solve (it staged without proposing — decision
                # 2026-11-15 — or state moved since its dry run): the template explains it.
                narration = committed.narration
        elif proposed:
            # Re-showing the unchanged pending proposal: its card, re-proposed only if the
            # stored solve differs from the one the model just narrated.
            changeset = await self._proposer.reshow(staging.mutations)
        reply = "".join(text).strip()
        if narration:
            sep = "\n\n" if reply else ""
            yield "delta", {"text": sep + narration}
            reply = f"{reply}{sep}{narration}"
        yield "done", {"outcome": "completed", "reply": reply or FALLBACK, "changeset": changeset}


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

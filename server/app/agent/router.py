"""`POST /agent/message` (contracts: "SSE: POST /agent/message").

The only code path that invokes the LLM, and only for a request whose transcript ends with a
user message (ARCH §8: never without a user action). Pre-stream failures are JSON errors;
once the 200 is sent, every outcome is an SSE event and exactly one `done` or `error` ends
the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.context import load_turn
from app.agent.loop import AgentTurn, Committed, Event, StaleBase
from app.agent.narration import narrate, project_names
from app.agent.prompt import render_snapshot
from app.agent.settings import AgentSettings
from app.agent.staging import ToolError
from app.api.deps import (
    DeviceTz,
    Maker,
    Now,
    RawBody,
    UserId,
    parse_body,
    transaction,
    validation_failed,
)
from app.api.views import changeset_view
from app.db.models import Changeset
from app.db.models.staged import InvalidMutation, StagedMutation, dump_staged
from app.db.write_paths import WritePathError, lock_user, propose_changeset, reject_changeset
from app.errors import AppError

router = APIRouter(tags=["agent"])


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=8000)]
    changeset_id: uuid.UUID | None = None


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[_Message], Field(min_length=1, max_length=200)]


_REQUEST: TypeAdapter[_Request] = TypeAdapter(_Request)


@router.post("/agent/message")
async def post_agent_message(
    request: Request, user_id: UserId, maker: Maker, now: Now, tz: DeviceTz, body: RawBody
) -> StreamingResponse:
    req = parse_body(_REQUEST, body)
    last = len(req.messages) - 1
    if req.messages[last].role != "user":
        raise validation_failed(
            [{"path": f"messages[{last}].role", "message": "expected 'user'"}],
            "The last transcript message must have role 'user'.",
        )
    async with transaction(maker) as tx:
        ctx = await load_turn(tx, user_id, now=now, device_tz=tz.key)
    settings = agent_settings(request)
    turn = AgentTurn(
        client=anthropic_client(request),
        settings=settings,
        transcript=req.messages,
        staging=ctx.staging,
        snapshot=render_snapshot(ctx.staging, ctx.snapshot),
        proposer=DbProposer(
            maker,
            user_id,
            now=now,
            device_tz=tz.key,
            committed_names=ctx.committed_names,
            pending_id=ctx.pending_id,
        ),
    )
    try:
        # Bounded, retries included: the request is not streaming yet, so a client that
        # gives up during an upstream stall would otherwise leave this call running.
        await asyncio.wait_for(turn.open(), settings.open_timeout_seconds)
    except (anthropic.APIError, TimeoutError) as exc:
        raise AppError(503, "agent_unavailable", _UNAVAILABLE_PRE) from exc
    return StreamingResponse(
        sse(turn.events(), heartbeat=settings.heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_UNAVAILABLE_PRE = "The assistant is temporarily unavailable. Try again in a moment."


# ---------------------------------------------------------------- seams (app.state)


def agent_settings(request: Request) -> AgentSettings:
    settings = getattr(request.app.state, "agent_settings", None)
    if settings is None:
        settings = request.app.state.agent_settings = AgentSettings.from_env()
    return settings


def anthropic_client(request: Request) -> anthropic.AsyncAnthropic:
    """One client per process (connection pooling); tests install a mock-transport one."""
    client = getattr(request.app.state, "anthropic_client", None)
    if client is None:
        client = request.app.state.anthropic_client = anthropic.AsyncAnthropic(
            timeout=anthropic.Timeout(60.0, connect=5.0), max_retries=2
        )
    return client


# ---------------------------------------------------------------- SSE


def frame(event: Event) -> str:
    name, data = event
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def sse(events: AsyncIterator[Event], *, heartbeat: float) -> AsyncIterator[str]:
    """Frame events; emit `: ping` whenever `heartbeat` seconds pass without one."""
    it = aiter(events)
    pending: asyncio.Task[Event] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(it))
            done, _ = await asyncio.wait({pending}, timeout=heartbeat)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield frame(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()  # client went away mid-turn: stop the model loop
            with contextlib.suppress(BaseException):
                await pending
        await it.aclose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------- proposals


class DbProposer:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        *,
        now: datetime,
        device_tz: str,
        committed_names: dict[str, str],
        pending_id: uuid.UUID | None,
    ) -> None:
        self._maker = maker
        self._user_id = user_id
        self._now = now  # one clock for dry run and commit → identical solves
        self._device_tz = device_tz
        self._names = committed_names
        self._pending_id = pending_id
        self._last_dry_run: str | None = None  # fingerprint of the solve the model last saw

    async def dry_run(self, staged: Sequence[StagedMutation]) -> str:
        if not staged:
            raise ToolError("nothing is staged, so there is nothing to propose")
        async with self._maker() as session:
            tx = await session.begin()
            try:
                view, narration = await self._propose(session, staged)
            except (InvalidMutation, WritePathError) as exc:
                raise ToolError(f"cannot propose: {exc}") from None
            finally:
                await tx.rollback()  # hypothetical: the real write is commit()
        self._last_dry_run = _fingerprint(staged, view)
        status = "does not fully fit" if (view.get("plan") or {}).get("infeasibility") else "fits"
        return f"Proposed schedule ({status}; shown to the user for confirmation): {narration}"

    async def commit(self, staged: Sequence[StagedMutation]) -> Committed:
        async with transaction(self._maker) as tx:
            await lock_user(tx, self._user_id)
            if await _pending_id(tx, self._user_id) != self._pending_id:
                raise StaleBase()
            if not staged:
                # Everything pending was discarded: retire the pending changeset.
                if self._pending_id is not None:
                    await reject_changeset(tx, self._user_id, self._pending_id, now=self._now)
                return Committed(None, None, matches_dry_run=True)
            view, narration = await self._propose(tx, staged)
        return Committed(
            view, narration, matches_dry_run=_fingerprint(staged, view) == self._last_dry_run
        )

    async def reshow(self, staged: Sequence[StagedMutation]) -> dict[str, Any] | None:
        if self._pending_id is None:
            return None
        async with transaction(self._maker) as tx:
            await lock_user(tx, self._user_id)
            if await _pending_id(tx, self._user_id) != self._pending_id:
                raise StaleBase()
            view = await changeset_view(tx, self._user_id, self._pending_id)
        if _fingerprint(staged, view) == self._last_dry_run:
            return view
        return (await self.commit(staged)).changeset

    async def _propose(
        self, tx: AsyncSession, staged: Sequence[StagedMutation]
    ) -> tuple[dict[str, Any], str]:
        # TODO(T4.2): pass freeBusy intervals; agent solves run with busy=[] until then.
        changeset_id = await propose_changeset(tx, self._user_id, staged, now=self._now)
        view = await changeset_view(tx, self._user_id, changeset_id)
        plan = view["plan"]
        narration = narrate(
            staged=staged,
            plan=plan,
            diff=view["diff"],
            names=project_names(staged, self._names),
            timezone=plan["timezone"] if plan else self._device_tz,
        )
        return view, narration


async def _pending_id(tx: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await tx.execute(
            select(Changeset.id)
            .where(Changeset.user_id == user_id, Changeset.status == "pending")
            .order_by(Changeset.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _fingerprint(staged: Sequence[StagedMutation], view: dict[str, Any]) -> str:
    """What the user is shown, minus row ids that differ between dry run and commit."""
    plan, diff = view["plan"], view["diff"]
    return json.dumps(
        {
            "staged": dump_staged(staged),
            "blocks": None
            if plan is None
            else [(b["project_id"], b["span"]) for b in plan["blocks"]],
            "infeasibility": None if plan is None else plan["infeasibility"],
            "diff": None
            if diff is None
            else [(e["project_id"], e["before"], e["after"], e["change"]) for e in diff["entries"]],
        },
        sort_keys=True,
    )

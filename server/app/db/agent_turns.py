"""The only write path for `agent_turns` (T6.2 eval capture; ARCH §8 "Quality & cost
guardrails").

One row per POST /agent/message turn. Token counts, outcome, staged result and ids are always
stored; `user_message` and `snapshot` are whatever the caller passes, which is None unless
AGENT_CAPTURE_TURNS is on (the agent layer decides; this module stores what it is given).

Retention (`RETENTION`, 30 days) is enforced on write, not by a job: each insert also deletes
EVERY user's rows with `created_at < now - RETENTION`, so a user who stops using the agent
still has their captured text aged out by anyone's next turn (served by
`agent_turns_created_at_idx`). Boundary: a row exactly 30 days old is KEPT (strict `<`); it
goes on the next turn after that. Retention therefore lags by the gap between turns
app-wide; with zero agent traffic nothing is purged, which is acceptable because then
nothing new is captured either.

Transaction discipline matches write_paths: the caller owns `tx` and commits; nothing reads
the clock (`now` is injected and stamps `created_at`). No advisory lock is taken: the row is
an append-only log, not scheduling state, and the purge only touches expired log rows that
nothing reads; concurrent purges deleting the same row just find it gone (no error).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import BigInteger, cast, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentTurnRow

__all__ = [
    "RETENTION",
    "AgentTurnOutcome",
    "AgentTurnRecord",
    "record_agent_turn",
    "tokens_used_since",
]

# Must equal app.db.models.agent.AGENT_TURN_OUTCOMES (asserted in tests/db/test_agent_turns.py).
AgentTurnOutcome = Literal["completed", "refused", "error"]

RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class AgentTurnRecord:
    user_id: uuid.UUID
    model: str
    outcome: AgentTurnOutcome
    error_code: str | None
    stop_reason: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    transcript_len: int
    staged: list[dict[str, Any]]  # JSON-ready staged mutations, same shape as changesets.staged
    changeset_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    user_message: str | None
    snapshot: str | None


async def record_agent_turn(tx: AsyncSession, record: AgentTurnRecord, *, now: datetime) -> None:
    """Insert the row, then delete every user's agent_turns rows older than now - RETENTION
    (same transaction). Caller owns the transaction and commit."""
    await tx.execute(
        insert(AgentTurnRow).values(
            user_id=record.user_id,
            created_at=now,
            model=record.model,
            outcome=record.outcome,
            error_code=record.error_code,
            stop_reason=record.stop_reason,
            model_calls=record.model_calls,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cache_read_tokens=record.cache_read_tokens,
            cache_write_tokens=record.cache_write_tokens,
            transcript_len=record.transcript_len,
            staged=record.staged,
            changeset_id=record.changeset_id,
            plan_id=record.plan_id,
            user_message=record.user_message,
            snapshot=record.snapshot,
        )
    )
    await tx.execute(delete(AgentTurnRow).where(AgentTurnRow.created_at < now - RETENTION))


async def tokens_used_since(tx: AsyncSession, user_id: uuid.UUID, since: datetime) -> int:
    """Sum of input+output+cache_write tokens for the user's turns with created_at >= since
    (for the future daily budget; cache reads excluded)."""
    total = func.coalesce(
        func.sum(
            cast(AgentTurnRow.input_tokens, BigInteger)
            + AgentTurnRow.output_tokens
            + AgentTurnRow.cache_write_tokens
        ),
        0,
    )
    stmt = select(total).where(AgentTurnRow.user_id == user_id, AgentTurnRow.created_at >= since)
    return int((await tx.execute(stmt)).scalar_one())

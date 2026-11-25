"""The only write path for `agent_turn_leases`: one agent turn in flight per user (issue #3;
ARCH §8 "Quality & cost guardrails").

`POST /agent/message` acquires a lease before it does any work and releases it on every exit.
The row is the single source of truth, so the limit holds across processes and machines; there
is no in-process registry.

Acquire is one atomic statement: an insert that, on conflict, takes over the existing row only
when it has expired (`expires_at <= now`). Concurrent acquires for one user serialize on the
primary key, so exactly one gets a token. A holder that dies mid-turn blocks the user until its
`expires_at` and no longer. Release deletes by `(user_id, token)`: a holder whose lease expired
and was taken over never deletes the new holder's row.

Transaction discipline matches write_paths: the caller owns `tx` and commits, in a short
transaction of its own; nothing reads the clock (`now` is injected). No advisory lock is taken
(the turn's own commit takes `lock_user` on the same key) and no connection is held while the
turn runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentTurnLease

__all__ = ["acquire_turn_lease", "release_turn_lease"]


async def acquire_turn_lease(
    tx: AsyncSession, user_id: uuid.UUID, *, now: datetime, ttl: timedelta
) -> uuid.UUID | None:
    """A new lease token valid until now + ttl, or None while an unexpired lease exists."""
    stmt = insert(AgentTurnLease).values(user_id=user_id, token=uuid.uuid4(), expires_at=now + ttl)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AgentTurnLease.user_id],
        set_={"token": stmt.excluded.token, "expires_at": stmt.excluded.expires_at},
        where=AgentTurnLease.expires_at <= now,
    ).returning(AgentTurnLease.token)
    return (await tx.execute(stmt)).scalar_one_or_none()


async def release_turn_lease(tx: AsyncSession, user_id: uuid.UUID, token: uuid.UUID) -> None:
    """Delete the lease if `token` still holds it; a no-op once another holder took over."""
    await tx.execute(
        delete(AgentTurnLease).where(
            AgentTurnLease.user_id == user_id, AgentTurnLease.token == token
        )
    )

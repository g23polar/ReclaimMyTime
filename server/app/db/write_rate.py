"""The only write path for `write_rate_counters`: the per-user write rate limit (issue #23).

Every scheduling write route (app.api.rate_limit) takes a slot before it does any work. The
row is the single source of truth, so the limit holds across processes and machines; there is
no in-process counter.

Fixed windows aligned to the Unix epoch, one row per user. Taking a slot is one atomic
statement: an insert that, on conflict, restarts the count at 1 when the stored window is
older than the current one and otherwise increments it, but only while it is below `limit`.
Concurrent takes for one user serialize on the primary key, so exactly `limit` succeed per
window. A refused take writes nothing.

Transaction discipline matches write_paths: the caller owns `tx` and commits, in a short
transaction of its own; nothing reads the clock (`now` is injected). No advisory lock is taken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WriteRateCounter

__all__ = ["take_write_slot", "window_start"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def window_start(now: datetime, window: timedelta) -> datetime:
    """`now` floored to a whole number of `window`s since the Unix epoch."""
    return now - (now - _EPOCH) % window


async def take_write_slot(
    tx: AsyncSession, user_id: uuid.UUID, *, now: datetime, limit: int, window: timedelta
) -> bool:
    """True when the write is within `limit` for the current window (and counted), else False."""
    start = window_start(now, window)
    stored = WriteRateCounter.window_start
    stmt = insert(WriteRateCounter).values(user_id=user_id, window_start=start, count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[WriteRateCounter.user_id],
        set_={
            "window_start": stmt.excluded.window_start,
            "count": case((stored < start, 1), else_=WriteRateCounter.count + 1),
        },
        where=or_(stored < start, WriteRateCounter.count < limit),
    ).returning(WriteRateCounter.user_id)
    return (await tx.execute(stmt)).scalar_one_or_none() is not None

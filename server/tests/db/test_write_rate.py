"""write_rate_counters (issue #23): the per-user write rate limit. Two independent engines on
one schema stand in for two server machines; the row is the only shared state."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import User
from app.db.write_rate import take_write_slot, window_start
from tests.db.conftest import T0, make_engine, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
WINDOW = timedelta(seconds=60)
LIMIT = 3


@pytest.fixture
async def other(engine: AsyncEngine, schema: str) -> AsyncIterator[Maker]:
    """A second machine: its own engine (and connection pool) on the same schema."""
    eng = await make_engine(schema)
    try:
        yield async_sessionmaker(eng, expire_on_commit=False)
    finally:
        await eng.dispose()


async def new_user(maker: Maker) -> uuid.UUID:
    uid = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=uid, created_at=T0))
    return uid


async def take(maker: Maker, user: uuid.UUID, now: datetime = T0, limit: int = LIMIT) -> bool:
    async with maker() as s, s.begin():
        return await take_write_slot(s, user, now=now, limit=limit, window=WINDOW)


async def counter(maker: Maker, user: uuid.UUID) -> list[tuple[datetime, int]]:
    return await rows(
        maker, "SELECT window_start, count FROM write_rate_counters WHERE user_id = :u", u=user
    )


def test_window_start_floors_to_the_epoch_grid() -> None:
    assert window_start(T0, WINDOW) == T0  # T0 is on a whole minute
    assert window_start(T0 + timedelta(seconds=59.9), WINDOW) == T0
    assert window_start(T0 + WINDOW, WINDOW) == T0 + WINDOW
    seven = timedelta(seconds=7)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert (window_start(T0, seven) - epoch) % seven == timedelta(0)
    assert timedelta(0) <= T0 - window_start(T0, seven) < seven


async def test_accepts_up_to_the_limit_then_refuses(maker: Maker, user_id: uuid.UUID) -> None:
    assert [await take(maker, user_id) for _ in range(LIMIT)] == [True] * LIMIT
    assert await counter(maker, user_id) == [(T0, LIMIT)]
    assert await take(maker, user_id, T0 + timedelta(seconds=59)) is False
    assert await counter(maker, user_id) == [(T0, LIMIT)]  # a refusal writes nothing


async def test_next_window_restarts_the_count(maker: Maker, user_id: uuid.UUID) -> None:
    for _ in range(LIMIT):
        assert await take(maker, user_id)
    assert await take(maker, user_id, T0 + WINDOW)
    assert await counter(maker, user_id) == [(T0 + WINDOW, 1)]
    # A later window, not the next one, restarts too; the row is reused, never added to.
    assert await take(maker, user_id, T0 + 10 * WINDOW + timedelta(seconds=5))
    assert await counter(maker, user_id) == [(T0 + 10 * WINDOW, 1)]
    assert len(await rows(maker, "SELECT 1 FROM write_rate_counters")) == 1


async def test_users_are_counted_apart(maker: Maker, other: Maker, user_id: uuid.UUID) -> None:
    b = await new_user(maker)
    for _ in range(LIMIT):
        assert await take(maker, user_id)
    assert await take(other, user_id) is False
    assert await take(other, b)
    assert await counter(maker, b) == [(T0, 1)]
    assert await counter(maker, user_id) == [(T0, LIMIT)]


async def test_concurrent_takes_accept_exactly_the_limit(
    maker: Maker, other: Maker, user_id: uuid.UUID
) -> None:
    limit = 5
    callers = [take(m, user_id, limit=limit) for m in (maker, other) * 6]  # 12 > limit
    got = await asyncio.gather(*callers)
    assert sum(got) == limit
    assert await counter(maker, user_id) == [(T0, limit)]


async def test_concurrent_takes_across_a_window_change(
    maker: Maker, other: Maker, user_id: uuid.UUID
) -> None:
    for _ in range(LIMIT):
        assert await take(maker, user_id)
    later = T0 + WINDOW
    got = await asyncio.gather(*(take(m, user_id, later) for m in (maker, other) * 4))
    assert sum(got) == LIMIT
    assert await counter(maker, user_id) == [(later, LIMIT)]


async def test_row_goes_with_the_user(maker: Maker, user_id: uuid.UUID) -> None:
    assert await take(maker, user_id)
    async with maker() as s, s.begin():
        await s.execute(User.__table__.delete().where(User.id == user_id))
    assert await counter(maker, user_id) == []

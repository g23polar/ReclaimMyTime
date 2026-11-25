"""agent_turn_leases (issue #3): one agent turn in flight per user. Two independent engines on
one schema stand in for two server machines; the row is the only shared state."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import User
from app.db.turn_leases import acquire_turn_lease, release_turn_lease
from tests.db.conftest import T0, make_engine, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
TTL = timedelta(seconds=180)


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


async def acquire(maker: Maker, user: uuid.UUID, now: datetime = T0) -> uuid.UUID | None:
    async with maker() as s, s.begin():
        return await acquire_turn_lease(s, user, now=now, ttl=TTL)


async def release(maker: Maker, user: uuid.UUID, token: uuid.UUID) -> None:
    async with maker() as s, s.begin():
        await release_turn_lease(s, user, token)


async def test_one_lease_per_user_across_engines(
    maker: Maker, other: Maker, user_id: uuid.UUID
) -> None:
    b = await new_user(maker)
    first = await acquire(maker, user_id)
    assert first is not None
    assert await acquire(other, user_id) is None
    assert await acquire(other, b) is not None  # another user's lease never blocks this one
    [(token, expires)] = await rows(
        maker, "SELECT token, expires_at FROM agent_turn_leases WHERE user_id = :u", u=user_id
    )
    assert (token, expires) == (first, T0 + TTL)

    await release(maker, user_id, first)
    assert await acquire(other, user_id) is not None


async def test_dead_holder_expires_at_ttl(maker: Maker, other: Maker, user_id: uuid.UUID) -> None:
    old = await acquire(maker, user_id)  # this machine dies and never releases
    assert old is not None
    assert await acquire(other, user_id, now=T0 + TTL - timedelta(seconds=1)) is None
    new = await acquire(other, user_id, now=T0 + TTL)
    assert new is not None and new != old

    # The dead holder comes back and releases its stale token: the new lease stays.
    await release(maker, user_id, old)
    left = await rows(maker, "SELECT token FROM agent_turn_leases WHERE user_id = :u", u=user_id)
    assert left == [(new,)]
    assert await acquire(maker, user_id, now=T0 + TTL) is None


async def test_concurrent_acquires_give_one_token(
    maker: Maker, other: Maker, user_id: uuid.UUID
) -> None:
    got = await asyncio.gather(acquire(maker, user_id), acquire(other, user_id))
    assert sum(t is not None for t in got) == 1


async def test_concurrent_acquires_after_expiry_give_one_token(
    maker: Maker,
    other: Maker,
    user_id: uuid.UUID,
) -> None:
    assert await acquire(maker, user_id) is not None
    later = T0 + TTL
    got = await asyncio.gather(acquire(maker, user_id, later), acquire(other, user_id, later))
    assert sum(t is not None for t in got) == 1


async def test_release_of_unknown_token_is_a_no_op(maker: Maker, user_id: uuid.UUID) -> None:
    token = await acquire(maker, user_id)
    await release(maker, user_id, uuid.uuid4())
    left = await rows(maker, "SELECT token FROM agent_turn_leases WHERE user_id = :u", u=user_id)
    assert left == [(token,)]

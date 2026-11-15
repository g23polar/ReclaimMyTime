from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.errors import UnauthenticatedError
from app.auth.repository import InMemoryAuthRepository, SessionRecord, UserRecord
from app.auth.service import AuthService
from app.auth.sessions import SessionPolicy, hash_token, mint_token
from tests.auth.conftest import T0
from tests.auth.fixtures import FrozenClock

POLICY = SessionPolicy(
    sliding=timedelta(days=30), absolute=timedelta(days=180), last_seen_interval=timedelta(hours=1)
)


def test_token_is_256_bit_and_unique() -> None:
    tokens = {mint_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) == 43 for t in tokens)  # 32 bytes urlsafe-b64, no padding


def test_hash_is_sha256_hex() -> None:
    h = hash_token("abc")
    assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_expiry_sliding_then_capped() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    assert POLICY.expiry(created, created) == created + timedelta(days=30)
    later = created + timedelta(days=100)
    assert POLICY.expiry(created, later) == later + timedelta(days=30)
    near_cap = created + timedelta(days=170)
    assert POLICY.expiry(created, near_cap) == created + timedelta(days=180)


def test_is_expired_boundary() -> None:
    exp = datetime(2026, 1, 31, tzinfo=UTC)
    assert not POLICY.is_expired(exp, exp - timedelta(microseconds=1))
    assert POLICY.is_expired(exp, exp)


def test_needs_touch() -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert POLICY.needs_touch(None, t)
    assert not POLICY.needs_touch(t, t + timedelta(minutes=59, seconds=59))
    assert POLICY.needs_touch(t, t + timedelta(hours=1))


async def _seed(repo: InMemoryAuthRepository) -> tuple[str, SessionRecord]:
    user = UserRecord(id=uuid.uuid4(), email=None, created_at=T0)
    await repo.create_user(user)
    token = mint_token()
    session = SessionRecord(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(token),
        created_at=T0,
        expires_at=T0 + timedelta(days=30),
        last_seen_at=T0,
    )
    await repo.create_session(session)
    return token, session


async def test_last_seen_hourly_throttle(
    service: AuthService, repo: InMemoryAuthRepository, clock: FrozenClock
) -> None:
    token, session = await _seed(repo)

    clock.advance(timedelta(minutes=30))
    await service.authenticate(token)
    stored = repo.sessions[session.id]
    assert stored.last_seen_at == T0  # not written within the hour
    assert stored.expires_at == T0 + timedelta(days=30)

    clock.advance(timedelta(minutes=30))  # exactly 1h since last write
    await service.authenticate(token)
    stored = repo.sessions[session.id]
    assert stored.last_seen_at == T0 + timedelta(hours=1)
    assert stored.expires_at == T0 + timedelta(hours=1, days=30)

    clock.advance(timedelta(minutes=10))
    await service.authenticate(token)
    assert repo.sessions[session.id].last_seen_at == T0 + timedelta(hours=1)


async def test_sliding_keeps_active_session_alive_until_absolute_cap(
    service: AuthService, repo: InMemoryAuthRepository, clock: FrozenClock
) -> None:
    token, session = await _seed(repo)
    # Use the session every 20 days: each use slides expiry forward 30 days...
    for _ in range(8):
        clock.advance(timedelta(days=20))
        await service.authenticate(token)
    # ...160 days in, expiry is capped at created_at + 180d.
    assert repo.sessions[session.id].expires_at == T0 + timedelta(days=180)
    clock.set(T0 + timedelta(days=180) - timedelta(seconds=1))
    await service.authenticate(token)
    clock.set(T0 + timedelta(days=180))
    with pytest.raises(UnauthenticatedError):
        await service.authenticate(token)


async def test_idle_session_expires_after_sliding_window(
    service: AuthService, repo: InMemoryAuthRepository, clock: FrozenClock
) -> None:
    token, _ = await _seed(repo)
    clock.advance(timedelta(days=30))
    with pytest.raises(UnauthenticatedError):
        await service.authenticate(token)


async def test_unknown_token_rejected(service: AuthService, repo: InMemoryAuthRepository) -> None:
    await _seed(repo)
    with pytest.raises(UnauthenticatedError):
        await service.authenticate(mint_token())


async def test_touch_is_monotonic_under_out_of_order_writes(
    repo: InMemoryAuthRepository,
) -> None:
    _, session = await _seed(repo)
    later, earlier = T0 + timedelta(hours=3), T0 + timedelta(hours=2)
    # Threshold far in the past: isolates GREATEST from the interval guard.
    await repo.touch_session(
        session.id, last_seen_at=later, expires_at=later + timedelta(days=30), threshold=later
    )
    # A slower request that read the session earlier writes its older values last.
    await repo.touch_session(
        session.id, last_seen_at=earlier, expires_at=earlier + timedelta(days=30), threshold=later
    )
    stored = repo.sessions[session.id]
    assert stored.last_seen_at == later
    assert stored.expires_at == later + timedelta(days=30)


async def test_concurrent_overdue_touches_write_once(
    repo: InMemoryAuthRepository,
) -> None:
    _, session = await _seed(repo)  # last_seen_at = T0
    writes: list[bool] = []
    interval = timedelta(hours=1)
    now = T0 + timedelta(hours=2)

    # Two requests both read the session as overdue, then both try to touch it.
    for _ in range(2):
        writes.append(
            await repo.touch_session(
                session.id,
                last_seen_at=now,
                expires_at=now + timedelta(days=30),
                threshold=now - interval,
            )
        )
    assert writes == [True, False]
    assert repo.sessions[session.id].last_seen_at == now


async def test_concurrent_authenticate_touches_write_once(
    service: AuthService, repo: InMemoryAuthRepository, clock: FrozenClock
) -> None:
    import asyncio

    token, session = await _seed(repo)
    clock.advance(timedelta(hours=2))
    calls: list[bool] = []
    original = repo.touch_session

    async def spy(*args, **kw) -> bool:  # noqa: ANN002, ANN003
        await asyncio.sleep(0)  # let the other reader reach its touch too
        wrote = await original(*args, **kw)
        calls.append(wrote)
        return wrote

    repo.touch_session = spy  # type: ignore[method-assign]
    await asyncio.gather(service.authenticate(token), service.authenticate(token))
    assert sorted(calls) == [False, True]
    assert repo.sessions[session.id].last_seen_at == T0 + timedelta(hours=2)

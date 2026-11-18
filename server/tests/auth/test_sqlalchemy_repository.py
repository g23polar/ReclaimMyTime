"""Integration tests for SqlAlchemyAuthRepository against real Postgres.

Skipped unless TEST_DATABASE_URL is set (e.g. postgresql+asyncpg://.../test); it must name a
test database (tests/dbguard.py). Tables are
created from the models (T2.1 owns the real migration) in a throwaway schema.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.repository import (
    EmailConflictError,
    IdentityRecord,
    SessionRecord,
    SqlAlchemyAuthRepository,
    UserRecord,
)
from app.db.base import Base
from app.db.models import auth as _models  # noqa: F401  (registers tables)
from tests.auth.conftest import T0
from tests.auth.fixtures import FrozenClock
from tests.dbguard import resolve_test_database_url

DATABASE_URL = resolve_test_database_url()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL
    schema = f"auth_test_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(
        DATABASE_URL, connect_args={"server_settings": {"search_path": f"{schema},public"}}
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.fixture
async def session(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as s:
        yield s
        await s.rollback()


async def _user(repo: SqlAlchemyAuthRepository, email: str | None = None) -> UserRecord:
    user = UserRecord(id=uuid.uuid4(), email=email, created_at=T0)
    await repo.create_user(user)
    return user


def _identity(user: UserRecord, **kw: object) -> IdentityRecord:
    base = dict(
        provider="apple",
        subject="sub-1",
        user_id=user.id,
        email=None,
        email_verified=False,
        is_private_email=True,
        apple_refresh_token_enc=b"ciphertext",
        created_at=T0,
    )
    base.update(kw)
    return IdentityRecord(**base)  # type: ignore[arg-type]


async def test_user_email_is_case_insensitive(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo, "Ada@Example.com")
    found = await repo.get_user_by_email("ada@example.COM")
    assert found is not None and found.id == user.id


async def test_identity_update_keeps_created_at_and_owner(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo)
    await repo.insert_identity(_identity(user))
    await repo.update_identity(
        "apple",
        "sub-1",
        apple_refresh_token_enc=None,
        email="x@y.z",
        email_verified=True,
        is_private_email=False,
    )
    got = await repo.get_identity("apple", "sub-1")
    assert got is not None
    assert (got.email, got.email_verified, got.is_private_email) == ("x@y.z", True, False)
    assert got.apple_refresh_token_enc == b"ciphertext"  # None keeps the stored credential
    assert got.created_at == T0 and got.user_id == user.id
    assert [i.subject for i in await repo.list_identities(user.id)] == ["sub-1"]


async def test_identity_update_without_email_keeps_email_metadata(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo)
    await repo.insert_identity(
        _identity(user, email="a@b.c", email_verified=True, is_private_email=False)
    )
    await repo.update_identity("apple", "sub-1", apple_refresh_token_enc=b"new", email=None)
    got = await repo.get_identity("apple", "sub-1")
    assert got is not None
    assert (got.email, got.email_verified, got.is_private_email) == ("a@b.c", True, False)
    assert got.apple_refresh_token_enc == b"new"


async def test_sessions_touch_and_revoke(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo)
    rec = SessionRecord(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="h" * 64,
        created_at=T0,
        expires_at=T0 + timedelta(days=30),
        last_seen_at=None,
    )
    await repo.create_session(rec)
    assert await repo.touch_session(
        rec.id, T0 + timedelta(hours=2), T0 + timedelta(days=31), T0 + timedelta(hours=1)
    )
    got = await repo.get_session_by_token_hash("h" * 64)
    assert got is not None and got.last_seen_at == T0 + timedelta(hours=2)
    assert got.expires_at == T0 + timedelta(days=31)
    await repo.delete_sessions_for_user(user.id)
    assert await repo.get_session_by_token_hash("h" * 64) is None


async def test_delete_user_cascades(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo)
    await repo.insert_identity(_identity(user))
    await repo.create_session(
        SessionRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash="c" * 64,
            created_at=T0,
            expires_at=T0 + timedelta(days=30),
            last_seen_at=None,
        )
    )
    await repo.delete_user(user.id)
    assert await repo.get_user(user.id) is None
    assert await repo.get_identity("apple", "sub-1") is None
    assert await repo.get_session_by_token_hash("c" * 64) is None


async def test_losing_insert_writes_nothing(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    owner, latecomer = await _user(repo), await _user(repo)
    assert await repo.insert_identity(_identity(owner)) == owner.id
    lost = _identity(latecomer, email="n@e.w", apple_refresh_token_enc=b"latecomer")
    assert await repo.insert_identity(lost) is None
    got = await repo.get_identity("apple", "sub-1")
    assert got is not None and got.user_id == owner.id
    assert got.email is None and got.apple_refresh_token_enc == b"ciphertext"


async def test_email_conflict_rolls_back_to_savepoint(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    first = await _user(repo, "taken@example.com")
    with pytest.raises(EmailConflictError):
        await _user(repo, "TAKEN@example.com")
    other = await _user(repo)  # transaction still usable
    with pytest.raises(EmailConflictError):
        await repo.set_user_email(other.id, "Taken@Example.com")
    assert (await repo.get_user(other.id)).email is None  # type: ignore[union-attr]
    assert (await repo.get_user(first.id)).email == "taken@example.com"  # type: ignore[union-attr]


async def test_touch_session_never_moves_backwards(session: AsyncSession) -> None:
    repo = SqlAlchemyAuthRepository(session)
    user = await _user(repo)
    rec = SessionRecord(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="t" * 64,
        created_at=T0,
        expires_at=T0 + timedelta(days=30),
        last_seen_at=None,
    )
    await repo.create_session(rec)
    later, earlier = T0 + timedelta(hours=3), T0 + timedelta(hours=2)
    await repo.touch_session(rec.id, later, later + timedelta(days=30), later)
    await repo.touch_session(rec.id, earlier, earlier + timedelta(days=30), later)
    got = await repo.get_session_by_token_hash("t" * 64)
    assert got is not None
    assert got.last_seen_at == later and got.expires_at == later + timedelta(days=30)


@pytest.mark.parametrize("index", ["sessions_user_id_idx", "identities_user_id_idx"])
async def test_user_id_indexes_exist(session: AsyncSession, index: str) -> None:
    rows = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND indexname = :name"
        ),
        {"name": index},
    )
    (indexdef,) = rows.one()
    assert "(user_id)" in indexdef


async def test_concurrent_overdue_touches_write_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as s, s.begin():
        repo = SqlAlchemyAuthRepository(s)
        user = await _user(repo)
        rec = SessionRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash="o" * 64,
            created_at=T0,
            expires_at=T0 + timedelta(days=30),
            last_seen_at=T0,
        )
        await repo.create_session(rec)
    now = T0 + timedelta(hours=2)
    threshold = now - timedelta(hours=1)
    async with maker() as a, a.begin(), maker() as b, b.begin():
        first = await SqlAlchemyAuthRepository(a).touch_session(
            rec.id, now, now + timedelta(days=30), threshold
        )
        # b read the same overdue row; its UPDATE waits on a's row lock, then re-checks.
        second = asyncio.create_task(
            SqlAlchemyAuthRepository(b).touch_session(
                rec.id, now, now + timedelta(days=30), threshold
            )
        )
        await asyncio.sleep(0.2)
        assert not second.done()
        await a.commit()
        assert (first, await asyncio.wait_for(second, timeout=5)) == (True, False)


async def test_racing_first_insert_waits_then_does_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as s, s.begin():
        repo = SqlAlchemyAuthRepository(s)
        winner, loser = await _user(repo), await _user(repo)
    async with maker() as a, a.begin(), maker() as b, b.begin():
        assert await SqlAlchemyAuthRepository(a).insert_identity(_identity(winner)) == winner.id
        lost = asyncio.create_task(
            SqlAlchemyAuthRepository(b).insert_identity(
                _identity(loser, apple_refresh_token_enc=b"loser")
            )
        )
        await asyncio.sleep(0.2)
        assert not lost.done()  # waits on the uncommitted unique key
        await a.commit()
        assert await asyncio.wait_for(lost, timeout=5) is None
        got = await SqlAlchemyAuthRepository(b).get_identity("apple", "sub-1")
        assert got is not None and got.user_id == winner.id
        assert got.apple_refresh_token_enc == b"ciphertext"


async def test_lock_user_blocks_until_commit(maker: async_sessionmaker[AsyncSession]) -> None:
    user_id = uuid.uuid4()
    async with maker() as holder, holder.begin():
        await SqlAlchemyAuthRepository(holder).lock_user(user_id)

        async def contender() -> None:
            async with maker() as s, s.begin():
                await SqlAlchemyAuthRepository(s).lock_user(user_id)

        task = asyncio.create_task(contender())
        await asyncio.sleep(0.3)
        assert not task.done()
    await asyncio.wait_for(task, timeout=5)
    async with maker() as s, s.begin():  # other users are not serialized
        await asyncio.wait_for(SqlAlchemyAuthRepository(s).lock_user(uuid.uuid4()), timeout=5)


async def test_delete_account_with_per_step_transactions(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from cryptography.fernet import Fernet

    from app.auth.crypto import TokenCipher
    from app.auth.dependencies import sqlalchemy_repository_provider
    from app.auth.service import AccountDeletionService

    cipher = TokenCipher([(1, Fernet.generate_key().decode())])
    provider = sqlalchemy_repository_provider(maker)
    async with provider() as repo:
        user = await _user(repo)  # type: ignore[arg-type]
        await repo.insert_identity(_identity(user, apple_refresh_token_enc=cipher.encrypt("rt-1")))

    in_tx_during_revoke: list[bool] = []

    class Apple:
        revoked: list[str] = []

        async def exchange_code(self, code: str):  # noqa: ANN201
            raise NotImplementedError

        async def revoke_refresh_token(self, token: str) -> None:
            # The per-user lock is free: no transaction is holding it across this await.
            async with maker() as s, s.begin():
                got = await s.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(CAST(:u AS text)))"),
                    {"u": str(user.id)},
                )
                in_tx_during_revoke.append(not got.scalar_one())
            self.revoked.append(token)

    class NoGoogle:  # no google_connections row: never called
        async def revoke_token(self, token: str) -> None:
            raise AssertionError("no Google connection to revoke")

    apple = Apple()
    await AccountDeletionService(
        provider, apple=apple, cipher=cipher, google=NoGoogle(), clock=FrozenClock(T0)
    ).delete_account(user.id)
    assert apple.revoked == ["rt-1"] and in_tx_during_revoke == [False]
    async with provider() as repo:
        assert await repo.get_user(user.id) is None
        assert await repo.get_identity("apple", "sub-1") is None

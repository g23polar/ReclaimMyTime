"""Persistence boundary for auth.

`AuthRepository` is the only interface the service uses; it is deliberately narrow so the
SQLAlchemy implementation can be swapped (T2.1) without touching service code.
Transactions are owned by the caller (a `RepositoryProvider` yields a repository inside
one transaction: commit on success, rollback on error); implementations never commit.

Per-user serialization: `lock_user(user_id)` takes the same transaction-scoped advisory
lock as the changeset write paths (`pg_advisory_xact_lock(hashtext(user_id::text))`,
ARCHITECTURE.md §3 Semantics). Account deletion and sign-in both take it before reading
or writing that user's identities: an existing identity's credential is only ever written
by `update_identity` under its owner's lock, and `insert_identity` never touches a row it
did not create.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import (
    Insert,
    Select,
    TextClause,
    Update,
    bindparam,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.auth import Identity, User, UserSession
from app.db.models.google import GoogleConnection


class EmailConflictError(Exception):
    """users.email is already held by another user (UNIQUE). The write did not happen
    and the surrounding transaction is still usable."""


@dataclass(frozen=True)
class UserRecord:
    id: uuid.UUID
    email: str | None
    created_at: datetime


@dataclass(frozen=True)
class IdentityRecord:
    provider: str
    subject: str
    user_id: uuid.UUID
    email: str | None
    email_verified: bool
    is_private_email: bool
    apple_refresh_token_enc: bytes | None
    created_at: datetime


@dataclass(frozen=True)
class SessionRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime | None


@dataclass(frozen=True)
class GoogleCredentialRecord:
    """The slice of google_connections account deletion needs (T4.1)."""

    refresh_token_enc: bytes = field(repr=False)
    key_id: int
    status: str  # 'active' | 'revoked'
    google_email: str


class AuthRepository(Protocol):
    async def lock_user(self, user_id: uuid.UUID) -> None:
        """Block until this transaction holds the per-user lock; released at commit/rollback."""
        ...

    async def get_user(self, user_id: uuid.UUID) -> UserRecord | None: ...

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        """Case-insensitive (citext) lookup."""
        ...

    async def create_user(self, user: UserRecord) -> None:
        """EmailConflictError if `user.email` is held by another user."""
        ...

    async def set_user_email(self, user_id: uuid.UUID, email: str | None) -> None:
        """EmailConflictError if `email` is held by another user."""
        ...

    async def delete_user(self, user_id: uuid.UUID) -> None:
        """Delete the user; identities and sessions go with it (ON DELETE CASCADE)."""
        ...

    async def get_identity(self, provider: str, subject: str) -> IdentityRecord | None: ...

    async def list_identities(self, user_id: uuid.UUID) -> list[IdentityRecord]: ...

    async def insert_identity(self, identity: IdentityRecord) -> uuid.UUID | None:
        """Insert if (provider, subject) is absent (ON CONFLICT DO NOTHING).

        Returns `identity.user_id` when this call created the row, None when the identity
        already existed (another transaction won); the existing row is left untouched.
        """
        ...

    async def update_identity(
        self,
        provider: str,
        subject: str,
        *,
        apple_refresh_token_enc: bytes | None,
        email: str | None,
        email_verified: bool = False,
        is_private_email: bool = False,
    ) -> None:
        """Refresh an existing identity; the caller must hold its owner's `lock_user`.

        A None `apple_refresh_token_enc` keeps the stored credential. A None `email`
        keeps the stored email, email_verified, and is_private_email together; otherwise
        all three are replaced. user_id and created_at are never changed.
        """
        ...

    async def create_session(self, session: SessionRecord) -> None: ...

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None: ...

    async def touch_session(
        self,
        session_id: uuid.UUID,
        last_seen_at: datetime,
        expires_at: datetime,
        threshold: datetime,
    ) -> bool:
        """Write only if the persisted last_seen_at is NULL or < `threshold`; returns
        whether a row was written, so concurrent overdue readers produce one write.
        Monotonic: each column becomes max(stored, given)."""
        ...

    async def delete_session(self, session_id: uuid.UUID) -> None: ...

    async def delete_sessions_for_user(self, user_id: uuid.UUID) -> None: ...

    async def get_google_credential(self, user_id: uuid.UUID) -> GoogleCredentialRecord | None:
        """The user's google_connections credential, or None (T4.1, DELETE /me)."""
        ...

    async def mark_google_revoked(self, user_id: uuid.UUID, at: datetime) -> None:
        """status='revoked', revoked_at=`at`; the caller holds the owner's lock."""
        ...


# Yields a repository inside one transaction (commit on success, rollback on error).
RepositoryProvider = Callable[[], AbstractAsyncContextManager[AuthRepository]]


def _max_dt(stored: datetime | None, given: datetime) -> datetime:
    # Mirrors SQL GREATEST, which ignores NULLs.
    return given if stored is None else max(stored, given)


# --------------------------------------------------------------------------- in-memory


class InMemoryAuthRepository:
    """Dict-backed implementation for tests; emulates the DDL's uniques and cascades.

    Used directly it behaves like autocommit (`lock_user` is released immediately).
    `transaction()` yields a view whose `lock_user` holds a per-user `asyncio.Lock`
    until the block exits, mirroring `pg_advisory_xact_lock`. There is no rollback.
    """

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, UserRecord] = {}
        self.identities: dict[tuple[str, str], IdentityRecord] = {}
        self.sessions: dict[uuid.UUID, SessionRecord] = {}
        self.google: dict[uuid.UUID, GoogleCredentialRecord] = {}  # google_connections
        self.google_revoked_at: dict[uuid.UUID, datetime] = {}
        self.open_transactions = 0
        self.lock_log: list[uuid.UUID] = []  # every lock_user call, in order
        self._user_locks: dict[uuid.UUID, asyncio.Lock] = {}

    def _user_lock(self, user_id: uuid.UUID) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def is_user_locked(self, user_id: uuid.UUID) -> bool:
        lock = self._user_locks.get(user_id)
        return lock is not None and lock.locked()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AuthRepository]:
        tx = _InMemoryTransaction(self)
        self.open_transactions += 1
        try:
            yield tx  # type: ignore[misc]
        finally:
            self.open_transactions -= 1
            tx.release_locks()

    async def lock_user(self, user_id: uuid.UUID) -> None:
        self.lock_log.append(user_id)
        async with self._user_lock(user_id):
            pass

    async def get_user(self, user_id: uuid.UUID) -> UserRecord | None:
        return self.users.get(user_id)

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        needle = email.casefold()
        return next(
            (u for u in self.users.values() if u.email and u.email.casefold() == needle), None
        )

    async def create_user(self, user: UserRecord) -> None:
        if user.id in self.users:
            raise ValueError("duplicate users.id")
        if user.email and await self.get_user_by_email(user.email):
            raise EmailConflictError()
        self.users[user.id] = user

    async def set_user_email(self, user_id: uuid.UUID, email: str | None) -> None:
        if email:
            other = await self.get_user_by_email(email)
            if other and other.id != user_id:
                raise EmailConflictError()
        self.users[user_id] = replace(self.users[user_id], email=email)

    async def delete_user(self, user_id: uuid.UUID) -> None:
        self.users.pop(user_id, None)
        self.identities = {k: v for k, v in self.identities.items() if v.user_id != user_id}
        self.sessions = {k: v for k, v in self.sessions.items() if v.user_id != user_id}
        self.google.pop(user_id, None)
        self.google_revoked_at.pop(user_id, None)

    async def get_identity(self, provider: str, subject: str) -> IdentityRecord | None:
        return self.identities.get((provider, subject))

    async def list_identities(self, user_id: uuid.UUID) -> list[IdentityRecord]:
        return [i for i in self.identities.values() if i.user_id == user_id]

    async def insert_identity(self, identity: IdentityRecord) -> uuid.UUID | None:
        if identity.user_id not in self.users:
            raise ValueError("identities.user_id FK violation")
        key = (identity.provider, identity.subject)
        if key in self.identities:
            return None
        self.identities[key] = identity
        return identity.user_id

    async def update_identity(
        self,
        provider: str,
        subject: str,
        *,
        apple_refresh_token_enc: bytes | None,
        email: str | None,
        email_verified: bool = False,
        is_private_email: bool = False,
    ) -> None:
        key = (provider, subject)
        stored = self.identities.get(key)
        if stored is None:
            return
        if apple_refresh_token_enc is not None:
            stored = replace(stored, apple_refresh_token_enc=apple_refresh_token_enc)
        if email is not None:
            stored = replace(
                stored,
                email=email,
                email_verified=email_verified,
                is_private_email=is_private_email,
            )
        self.identities[key] = stored

    async def create_session(self, session: SessionRecord) -> None:
        if session.user_id not in self.users:
            raise ValueError("sessions.user_id FK violation")
        if any(s.token_hash == session.token_hash for s in self.sessions.values()):
            raise ValueError("duplicate sessions.token_hash")
        self.sessions[session.id] = session

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        return next((s for s in self.sessions.values() if s.token_hash == token_hash), None)

    async def touch_session(
        self,
        session_id: uuid.UUID,
        last_seen_at: datetime,
        expires_at: datetime,
        threshold: datetime,
    ) -> bool:
        stored = self.sessions.get(session_id)
        if stored is None or (stored.last_seen_at is not None and stored.last_seen_at >= threshold):
            return False
        self.sessions[session_id] = replace(
            stored,
            last_seen_at=_max_dt(stored.last_seen_at, last_seen_at),
            expires_at=_max_dt(stored.expires_at, expires_at),
        )
        return True

    async def delete_session(self, session_id: uuid.UUID) -> None:
        self.sessions.pop(session_id, None)

    async def delete_sessions_for_user(self, user_id: uuid.UUID) -> None:
        self.sessions = {k: v for k, v in self.sessions.items() if v.user_id != user_id}

    async def get_google_credential(self, user_id: uuid.UUID) -> GoogleCredentialRecord | None:
        return self.google.get(user_id)

    async def mark_google_revoked(self, user_id: uuid.UUID, at: datetime) -> None:
        stored = self.google.get(user_id)
        if stored is not None:
            self.google[user_id] = replace(stored, status="revoked")
            self.google_revoked_at[user_id] = at


class _InMemoryTransaction:
    """One transaction over an `InMemoryAuthRepository`: holds user locks until exit and
    delegates everything else to the store (so test spies on the store still apply)."""

    def __init__(self, store: InMemoryAuthRepository) -> None:
        self._store = store
        self._held: dict[uuid.UUID, asyncio.Lock] = {}

    async def lock_user(self, user_id: uuid.UUID) -> None:
        self._store.lock_log.append(user_id)
        if user_id in self._held:
            return  # re-entrant within a transaction, like the advisory lock
        lock = self._store._user_lock(user_id)
        await lock.acquire()
        self._held[user_id] = lock

    async def update_identity(self, provider: str, subject: str, **kw: Any) -> None:
        stored = await self._store.get_identity(provider, subject)
        if stored is not None:
            # Mirrors the service contract: credentials are written under the owner lock.
            assert stored.user_id in self._held, "update_identity without the owner lock"
        await self._store.update_identity(provider, subject, **kw)

    async def mark_google_revoked(self, user_id: uuid.UUID, at: datetime) -> None:
        assert user_id in self._held, "mark_google_revoked without the owner lock"
        await self._store.mark_google_revoked(user_id, at)

    def release_locks(self) -> None:
        for lock in self._held.values():
            lock.release()
        self._held.clear()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


# --------------------------------------------------------------------------- SQLAlchemy


def _user(row: User) -> UserRecord:
    return UserRecord(id=row.id, email=row.email, created_at=row.created_at)


def _identity(row: Identity) -> IdentityRecord:
    return IdentityRecord(
        provider=row.provider,
        subject=row.subject,
        user_id=row.user_id,
        email=row.email,
        email_verified=row.email_verified,
        is_private_email=row.is_private_email,
        apple_refresh_token_enc=row.apple_refresh_token_enc,
        created_at=row.created_at,
    )


def _session(row: UserSession) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
    )


USERS_EMAIL_CONSTRAINT = "uq_users_email"


def user_by_email_statement(email: str) -> Select[tuple[User]]:
    # Plain equality: CITEXT compares case-insensitively and uses the unique index.
    return select(User).where(User.email == email)


def touch_session_statement(
    session_id: uuid.UUID, last_seen_at: datetime, expires_at: datetime, threshold: datetime
) -> Update:
    # The interval check runs against the persisted row, so of several concurrent overdue
    # readers only the first UPDATE matches; the rest re-check after it commits and skip.
    return (
        update(UserSession)
        .where(
            UserSession.id == session_id,
            or_(UserSession.last_seen_at.is_(None), UserSession.last_seen_at < threshold),
        )
        .values(
            last_seen_at=func.greatest(UserSession.last_seen_at, last_seen_at),
            expires_at=func.greatest(UserSession.expires_at, expires_at),
        )
    )


def insert_identity_statement(values: dict[str, Any]) -> Insert:
    # DO NOTHING (not DO UPDATE): a losing first sign-in must not write the winner's
    # credential before holding the winner's per-user lock.
    return (
        pg_insert(Identity)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Identity.provider, Identity.subject])
        .returning(Identity.user_id)
    )


def user_lock_statement(user_id: uuid.UUID) -> TextClause:
    # Same key as the changeset write paths (ARCHITECTURE.md §3 Semantics).
    return text("SELECT pg_advisory_xact_lock(hashtext(CAST(:user_id AS uuid)::text))").bindparams(
        bindparam("user_id", user_id, type_=PG_UUID(as_uuid=True))
    )


def _is_email_conflict(exc: IntegrityError) -> bool:
    orig = exc.orig
    name = getattr(orig, "constraint_name", None)
    cause = getattr(orig, "__cause__", None)
    name = name or getattr(cause, "constraint_name", None)
    return name == USERS_EMAIL_CONSTRAINT or (name is None and USERS_EMAIL_CONSTRAINT in str(orig))


class SqlAlchemyAuthRepository:
    """Postgres implementation over an `AsyncSession` whose transaction the caller owns.

    Bulk deletes are Core statements so the database's ON DELETE CASCADE does the work
    (no ORM relationship cascades are configured).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def lock_user(self, user_id: uuid.UUID) -> None:
        await self._s.execute(user_lock_statement(user_id))

    async def get_user(self, user_id: uuid.UUID) -> UserRecord | None:
        # populate_existing: always SELECT, so a re-read after lock_user sees committed rows.
        row = await self._s.get(User, user_id, populate_existing=True)
        return _user(row) if row else None

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        row = (await self._s.execute(user_by_email_statement(email))).scalar_one_or_none()
        return _user(row) if row else None

    async def create_user(self, user: UserRecord) -> None:
        row = User(id=user.id, email=user.email, created_at=user.created_at)
        try:
            async with self._s.begin_nested():
                self._s.add(row)
        except IntegrityError as exc:
            if not _is_email_conflict(exc):
                raise
            # SAVEPOINT rolled back; the outer transaction is intact.
            raise EmailConflictError() from None

    async def set_user_email(self, user_id: uuid.UUID, email: str | None) -> None:
        try:
            async with self._s.begin_nested():
                await self._s.execute(update(User).where(User.id == user_id).values(email=email))
        except IntegrityError as exc:
            if not _is_email_conflict(exc):
                raise
            raise EmailConflictError() from None

    async def delete_user(self, user_id: uuid.UUID) -> None:
        await self._s.execute(delete(User).where(User.id == user_id))
        self._s.expunge_all()

    async def get_identity(self, provider: str, subject: str) -> IdentityRecord | None:
        row = await self._s.get(Identity, (provider, subject), populate_existing=True)
        return _identity(row) if row else None

    async def list_identities(self, user_id: uuid.UUID) -> list[IdentityRecord]:
        stmt = select(Identity).where(Identity.user_id == user_id).order_by(Identity.created_at)
        return [_identity(r) for r in (await self._s.execute(stmt)).scalars()]

    async def insert_identity(self, identity: IdentityRecord) -> uuid.UUID | None:
        stmt = insert_identity_statement(
            {
                "provider": identity.provider,
                "subject": identity.subject,
                "user_id": identity.user_id,
                "email": identity.email,
                "email_verified": identity.email_verified,
                "is_private_email": identity.is_private_email,
                "apple_refresh_token_enc": identity.apple_refresh_token_enc,
                "created_at": identity.created_at,
            }
        )
        owner = (await self._s.execute(stmt)).scalar_one_or_none()
        self._s.expire_all()
        return owner

    async def update_identity(
        self,
        provider: str,
        subject: str,
        *,
        apple_refresh_token_enc: bytes | None,
        email: str | None,
        email_verified: bool = False,
        is_private_email: bool = False,
    ) -> None:
        values: dict[str, Any] = {}
        if apple_refresh_token_enc is not None:
            values["apple_refresh_token_enc"] = apple_refresh_token_enc
        if email is not None:
            values.update(
                email=email, email_verified=email_verified, is_private_email=is_private_email
            )
        if not values:
            return
        await self._s.execute(
            update(Identity)
            .where(Identity.provider == provider, Identity.subject == subject)
            .values(**values)
        )
        self._s.expire_all()

    async def create_session(self, session: SessionRecord) -> None:
        self._s.add(
            UserSession(
                id=session.id,
                user_id=session.user_id,
                token_hash=session.token_hash,
                created_at=session.created_at,
                expires_at=session.expires_at,
                last_seen_at=session.last_seen_at,
            )
        )
        await self._s.flush()

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        stmt = select(UserSession).where(UserSession.token_hash == token_hash)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _session(row) if row else None

    async def touch_session(
        self,
        session_id: uuid.UUID,
        last_seen_at: datetime,
        expires_at: datetime,
        threshold: datetime,
    ) -> bool:
        stmt = touch_session_statement(session_id, last_seen_at, expires_at, threshold)
        result = await self._s.execute(stmt)
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_session(self, session_id: uuid.UUID) -> None:
        await self._s.execute(delete(UserSession).where(UserSession.id == session_id))

    async def delete_sessions_for_user(self, user_id: uuid.UUID) -> None:
        await self._s.execute(delete(UserSession).where(UserSession.user_id == user_id))

    async def get_google_credential(self, user_id: uuid.UUID) -> GoogleCredentialRecord | None:
        stmt = select(
            GoogleConnection.refresh_token_enc,
            GoogleConnection.key_id,
            GoogleConnection.status,
            GoogleConnection.google_email,
        ).where(GoogleConnection.user_id == user_id)
        row = (await self._s.execute(stmt)).first()
        if row is None:
            return None
        return GoogleCredentialRecord(
            refresh_token_enc=row[0], key_id=row[1], status=row[2], google_email=row[3]
        )

    async def mark_google_revoked(self, user_id: uuid.UUID, at: datetime) -> None:
        await self._s.execute(
            update(GoogleConnection)
            .where(GoogleConnection.user_id == user_id)
            .values(status="revoked", revoked_at=at)
        )

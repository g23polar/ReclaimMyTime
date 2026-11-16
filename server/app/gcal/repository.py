"""google_connections persistence over a caller-owned `AsyncSession` (never commits).

Writes that replace or delete a credential run under the per-user advisory lock
(`pg_advisory_xact_lock(hashtext(user_id::text))`, ARCH §3 Semantics) so they serialize
with account deletion (app.auth.service.AccountDeletionService), which re-reads this row
under the same lock before deleting the user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GoogleConnection, User
from app.db.write_paths import lock_user

__all__ = [
    "ConnectionRecord",
    "clear_app_calendar_id",
    "delete_connection",
    "get_connection",
    "lock_user",
    "mark_revoked",
    "same_account",
    "set_app_calendar_id",
    "upsert_connection",
    "user_exists",
]


@dataclass(frozen=True)
class ConnectionRecord:
    user_id: uuid.UUID
    google_email: str
    refresh_token_enc: bytes = field(repr=False)
    key_id: int
    scopes: tuple[str, ...]
    app_calendar_id: str | None
    status: str
    revoked_at: datetime | None
    created_at: datetime


def _record(row: GoogleConnection) -> ConnectionRecord:
    return ConnectionRecord(
        user_id=row.user_id,
        google_email=row.google_email,
        refresh_token_enc=row.refresh_token_enc,
        key_id=row.key_id,
        scopes=tuple(row.scopes),
        app_calendar_id=row.app_calendar_id,
        status=row.status,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


async def user_exists(s: AsyncSession, user_id: uuid.UUID) -> bool:
    return (await s.execute(select(User.id).where(User.id == user_id))).first() is not None


async def get_connection(s: AsyncSession, user_id: uuid.UUID) -> ConnectionRecord | None:
    stmt = (
        select(GoogleConnection)
        .where(GoogleConnection.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    row = (await s.execute(stmt)).scalar_one_or_none()
    return _record(row) if row is not None else None


def same_account(a: str, b: str) -> bool:
    """google_email is citext: compare case-insensitively."""
    return a.casefold() == b.casefold()


async def upsert_connection(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    previous: ConnectionRecord | None,
    google_email: str,
    refresh_token_enc: bytes,
    key_id: int,
    scopes: list[str],
    now: datetime,
) -> None:
    """Insert, or replace the credential of `previous` (read by the caller in this same
    transaction, under the lock).

    Reconnect with a different Google account clears `app_calendar_id` (that calendar
    lives in the other account; T4.2 recreates it). Same account (case-insensitive, like
    the citext column) keeps it. `created_at` is kept on reconnect.
    """
    values: dict[str, object] = {
        "google_email": google_email,
        "refresh_token_enc": refresh_token_enc,
        "key_id": key_id,
        "scopes": scopes,
        "status": "active",
        "revoked_at": None,
    }
    if previous is None:
        s.add(GoogleConnection(user_id=user_id, app_calendar_id=None, created_at=now, **values))
        await s.flush()
        return
    if not same_account(previous.google_email, google_email):
        values["app_calendar_id"] = None
    await s.execute(
        update(GoogleConnection).where(GoogleConnection.user_id == user_id).values(**values)
    )


async def mark_revoked(
    s: AsyncSession, user_id: uuid.UUID, *, refresh_token_enc: bytes, at: datetime
) -> bool:
    """Mark revoked only if the row still holds exactly this ciphertext (Fernet output is
    randomized, so a reconnect in the meantime never matches) and is still active."""
    result = await s.execute(
        update(GoogleConnection)
        .where(
            GoogleConnection.user_id == user_id,
            GoogleConnection.refresh_token_enc == refresh_token_enc,
            GoogleConnection.status == "active",
        )
        .values(status="revoked", revoked_at=at)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def delete_connection(s: AsyncSession, user_id: uuid.UUID) -> None:
    await s.execute(delete(GoogleConnection).where(GoogleConnection.user_id == user_id))


async def set_app_calendar_id(s: AsyncSession, user_id: uuid.UUID, calendar_id: str) -> bool:
    """Store the app calendar id only while none is stored and the row is active (T4.2).
    Callers hold the per-user lock and have re-read the row; the predicate is belt and braces
    against a second calendar ever being recorded."""
    result = await s.execute(
        update(GoogleConnection)
        .where(
            GoogleConnection.user_id == user_id,
            GoogleConnection.app_calendar_id.is_(None),
            GoogleConnection.status == "active",
        )
        .values(app_calendar_id=calendar_id)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def clear_app_calendar_id(s: AsyncSession, user_id: uuid.UUID, calendar_id: str) -> bool:
    """Forget `calendar_id` (Google said 404) — only if it is still the stored one, so a
    calendar recreated concurrently is never forgotten by a late report about the old one."""
    result = await s.execute(
        update(GoogleConnection)
        .where(
            GoogleConnection.user_id == user_id,
            GoogleConnection.app_calendar_id == calendar_id,
        )
        .values(app_calendar_id=None)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]

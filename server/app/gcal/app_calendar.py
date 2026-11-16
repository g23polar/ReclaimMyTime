"""The app-owned secondary calendar (T4.2; ARCH §3 "Write-back outbox").

`ensure_app_calendar(user_id)` returns the user's "ReclaimMyTime" calendar id, creating it
lazily (calendars.insert is allowed under calendar.app.created). Nothing calls it yet; the
T4.3 write-back worker will. Idempotent and safe under concurrency:

- the id is stored under the per-user advisory lock, after a re-read of the row;
- the Google call itself happens OUTSIDE the lock (same discipline as GcalService), so two
  concurrent creators can both create a calendar; the loser sees the winner's id on the
  re-read, keeps it, and best-effort deletes its own orphan via the API;
- `missing=<id>` is the 404 path: a caller whose write to that calendar got 404 (the user
  deleted it in Google) clears the stored id (compare-and-set) and a fresh one is created.

Raises the gcal domain errors (callers decide how to surface them): GcalConnectionInactiveError
(no active connection; GcalGrantRevokedError on invalid_grant), GcalTransientError (Google
unavailable, or the connection changed underneath), GcalCredentialUndecryptableError.
Calendar ids are not logged (they identify the account).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gcal import repository as repo
from app.gcal.calendar import APP_CALENDAR_SUMMARY, CalendarNotFoundError, GoogleCalendarClient
from app.gcal.client import GoogleUnavailableError
from app.gcal.errors import GcalConnectionInactiveError, GcalTransientError
from app.gcal.service import GcalService

logger = logging.getLogger(__name__)


class AppCalendarService:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        service: GcalService,
        calendar: GoogleCalendarClient,
    ) -> None:
        self._maker = maker
        self._service = service
        self._calendar = calendar

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as s, s.begin():
            yield s

    async def ensure_app_calendar(self, user_id: uuid.UUID, *, missing: str | None = None) -> str:
        if missing is not None:
            async with self._tx() as s:
                await repo.lock_user(s, user_id)
                if await repo.clear_app_calendar_id(s, user_id, missing):
                    logger.info("app calendar reported gone (404); stored id cleared")

        async with self._tx() as s:
            row = await repo.get_connection(s, user_id)
        if row is None or row.status != "active":
            raise GcalConnectionInactiveError()
        if row.app_calendar_id is not None:
            return row.app_calendar_id

        token = await self._service.access_token(user_id)
        try:
            created = await self._calendar.create_calendar(token.token, APP_CALENDAR_SUMMARY)
        except GoogleUnavailableError:
            raise GcalTransientError() from None

        async with self._tx() as s:
            await repo.lock_user(s, user_id)
            current = await repo.get_connection(s, user_id)
            usable = (
                current is not None
                and current.status == "active"
                and repo.same_account(current.google_email, row.google_email)
            )
            if usable and current is not None and current.app_calendar_id is None:
                await repo.set_app_calendar_id(s, user_id, created)
                logger.info("app calendar created and stored")
                return created
            kept = current.app_calendar_id if usable and current is not None else None

        # Not stored: a concurrent creator won, or the connection changed. Clean up our
        # calendar outside the lock; failures only leave an empty orphan calendar behind.
        await self._discard(token.token, created)
        if kept is not None:
            logger.info("app calendar created concurrently; kept the stored one")
            return kept
        if current is None or current.status != "active":
            raise GcalConnectionInactiveError()
        raise GcalTransientError()  # reconnected to another account meanwhile: retry

    async def _discard(self, access_token: str, calendar_id: str) -> None:
        try:
            await self._calendar.delete_calendar(access_token, calendar_id)
        except CalendarNotFoundError:
            return
        except Exception as exc:  # best effort by design
            logger.warning(
                "best-effort delete of orphan app calendar failed: %s", type(exc).__name__
            )

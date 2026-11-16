"""Google Calendar write-back outbox worker (T4.3a; ARCH §3 "Write-back outbox").

`WriteBack.drain(user_id)` drives the user's live blocks `gcal_status` pending → synced in the
app calendar, and deletes the events of lineages that left the live plan. It runs in-process:
`OutboxRegistry.kick` after every confirm (fire-and-forget), and `OutboxRegistry.wait` from
`POST /gcal/sync` (bounded). Anything left pending (crash, Fly stop, Google failure) is retried
by the next kick or sync; there is no other job.

Locking discipline (the T4.2 deadlock lesson; see tests/api/test_busy_pool.py):
- no Google call ever runs under the per-user advisory lock or inside an open transaction;
- work is claimed in one short transaction (app.db.outbox.claim_*), committed, then Google is
  called with NO session open, then each result is recorded by compare-and-set in another
  short transaction. Token minting and `ensure_app_calendar` open their own short sessions and
  may take the user lock; they are only ever called with nothing held.

Idempotency: event ids are deterministic per (lineage, plan week) (`event_id_for`), so a
create that answers 409 means the event already exists (an earlier attempt, another version of
the same lineage, or a deleted event being revived) and becomes an update. Updates are full
replaces with `status: confirmed`.

Privacy (CLAUDE.md rule 4 is about READING calendar contents; writing our own project names to
our own calendar is fine): event bodies and project names are never logged, and nothing here
feeds the LLM. Log lines carry counts and outcomes only.

Cross-process: one drain per user per PROCESS (the registry is in memory). v1 runs a single
Fly machine (same trigger as contracts O10); with several machines two drains could interleave
writes for one lineage out of order. See the T4.3 notes before scaling out.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.clock import Clock
from app.db import outbox
from app.db.outbox import OutboxItem
from app.gcal import repository as repo
from app.gcal.calendar import (
    CalendarNotFoundError,
    EventExistsError,
    EventNotFoundError,
    EventRejectedError,
)
from app.gcal.client import GoogleUnavailableError
from app.gcal.errors import (
    GcalConnectionInactiveError,
    GcalCredentialUndecryptableError,
    GcalGrantRevokedError,
    GcalTransientError,
)

if TYPE_CHECKING:
    from app.gcal.runtime import GcalRuntime

logger = logging.getLogger(__name__)

EVENT_DESCRIPTION = "Scheduled by ReclaimMyTime"
BACKGROUND_BUDGET_S = 120.0  # a kicked drain gives up (rows stay pending) after this long


class DrainStatus(StrEnum):
    DONE = "done"  # nothing claimable is left pending
    PARTIAL = "partial"  # out of time budget; the rest stays pending
    NOT_CONNECTED = "not_connected"  # never connected (or disconnected): nothing to write to
    RECONNECT_REQUIRED = "reconnect_required"  # revoked, or the credential is undecryptable
    UNAVAILABLE = "unavailable"  # Google failed; the rest stays pending


@dataclass(frozen=True)
class DrainResult:
    status: DrainStatus
    written: int = 0
    deleted: int = 0


class _Stop(Exception):
    def __init__(self, status: DrainStatus) -> None:
        self.status = status


def event_body(item: OutboxItem) -> dict[str, Any]:
    """The full event (events.update replaces it). Title = project name, fixed description,
    lineage and block ids in private extended properties (part (b) maps edits back by them)."""

    def when(dt: Any) -> dict[str, str]:
        return {"dateTime": dt.isoformat().replace("+00:00", "Z")}

    return {
        "summary": item.project_name,
        "description": EVENT_DESCRIPTION,
        "start": when(item.span.start),
        "end": when(item.span.end),
        "status": "confirmed",  # also revives an event deleted in Google (same id)
        "extendedProperties": {
            "private": {"lineage_id": str(item.lineage_id), "block_id": str(item.block_id)}
        },
    }


class WriteBack:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        runtime: Callable[[], GcalRuntime],
        clock: Clock,
        *,
        budget_s: float | None = BACKGROUND_BUDGET_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maker = maker
        self._runtime = runtime  # built only for a user who has a connection row
        self._clock = clock
        self._budget_s = budget_s
        self._monotonic = monotonic

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as s, s.begin():
            yield s

    async def drain(self, user_id: uuid.UUID) -> DrainResult:
        deadline = None if self._budget_s is None else self._monotonic() + self._budget_s
        progress = _Progress()
        try:
            await self._drain(user_id, deadline, progress)
            status = DrainStatus.DONE
        except _Stop as stop:
            status = stop.status
        result = DrainResult(status, written=progress.written, deleted=progress.deleted)
        if status is not DrainStatus.NOT_CONNECTED:
            logger.info(
                "gcal write-back: %s (%d written, %d deleted)",
                status.value,
                result.written,
                result.deleted,
            )
        return result

    def _check_time(self, deadline: float | None) -> None:
        if deadline is not None and self._monotonic() >= deadline:
            raise _Stop(DrainStatus.PARTIAL)

    async def _drain(self, user_id: uuid.UUID, deadline: float | None, p: _Progress) -> None:
        async with self._tx() as s:
            row = await repo.get_connection(s, user_id)
        if row is None:
            raise _Stop(DrainStatus.NOT_CONNECTED)
        if row.status != "active":
            raise _Stop(DrainStatus.RECONNECT_REQUIRED)
        # Reconnect mid-drain is not handled: only an account switch (ruled out by the user)
        # changes the calendar; a same-account reconnect keeps `app_calendar_id`.
        runtime = self._runtime()
        token = await self._google(user_id, runtime.service(self._maker).access_token(user_id))
        app_calendar = runtime.app_calendar(self._maker)
        if row.app_calendar_id is None:
            # About to be created (first connect, reconnect, or a 404 whose recreation did not
            # finish): nothing we wrote before is in it. Re-pend FIRST, committed, so a crash
            # after the new id is stored cannot leave rows `synced` against the old calendar
            # (review round 1); a crash before only leaves extra pending rows (idempotent).
            await self._repend(user_id)
        calendar_id = await self._google(user_id, app_calendar.ensure_app_calendar(user_id))

        calendar = runtime.calendar
        recreated = False
        skipped: set[uuid.UUID] = set()
        while True:
            self._check_time(deadline)
            async with self._tx() as s:
                items = await outbox.claim_upserts(s, user_id, self._clock.now(), exclude=skipped)
            if not items:
                break
            for item in items:
                self._check_time(deadline)
                try:
                    etag = await self._upsert(calendar, token.token, calendar_id, item)
                except CalendarNotFoundError:
                    if recreated:  # a second 404 right after recreating: give up for now
                        raise _Stop(DrainStatus.UNAVAILABLE) from None
                    recreated = True
                    logger.info("gcal write-back: app calendar gone (404); recreating")
                    # Re-pend before the replacement is stored (see above): durable first.
                    await self._repend(user_id)
                    calendar_id = await self._google(
                        user_id, app_calendar.ensure_app_calendar(user_id, missing=calendar_id)
                    )
                    break  # re-claim: everything not ended is pending again
                except EventRejectedError:
                    skipped.add(item.block_id)  # stays pending; retried by a later drain
                    continue
                except GoogleUnavailableError:
                    raise _Stop(DrainStatus.UNAVAILABLE) from None
                async with self._tx() as s:
                    await outbox.record_synced(
                        s, user_id, item.block_id, item.event_id, etag, item.project_name
                    )
                p.written += 1

        async with self._tx() as s:
            orphans = await outbox.claim_deletes(s, user_id, self._clock.now())
        for event_id in orphans:
            self._check_time(deadline)
            try:
                await calendar.delete_event(token.token, calendar_id, event_id)
            except EventRejectedError:
                continue
            except GoogleUnavailableError:
                raise _Stop(DrainStatus.UNAVAILABLE) from None
            async with self._tx() as s:
                await outbox.record_deleted(s, user_id, event_id)
            p.deleted += 1

    async def _upsert(
        self, calendar: Any, access_token: str, calendar_id: str, item: OutboxItem
    ) -> str:
        """Insert-or-update by the deterministic id; returns the etag. Update first when an
        earlier version of the lineage already wrote this event (a move), insert first
        otherwise. Insert's 404 is unambiguous (the calendar is gone) → CalendarNotFoundError."""
        body = event_body(item)
        if item.known_to_exist:
            try:
                return await calendar.update_event(access_token, calendar_id, item.event_id, body)
            except EventNotFoundError:
                pass  # deleted in Google, or the calendar is gone: the insert tells which
            try:
                return await calendar.insert_event(
                    access_token, calendar_id, {"id": item.event_id, **body}
                )
            except EventExistsError:
                raise EventRejectedError() from None  # raced with another writer: next drain
        try:
            return await calendar.insert_event(
                access_token, calendar_id, {"id": item.event_id, **body}
            )
        except EventExistsError:
            pass  # the deterministic id already exists: it is ours, update it
        try:
            return await calendar.update_event(access_token, calendar_id, item.event_id, body)
        except EventNotFoundError:
            raise EventRejectedError() from None  # deleted between the two calls: next drain

    async def _repend(self, user_id: uuid.UUID) -> None:
        async with self._tx() as s:
            n = await outbox.repend_current(s, user_id, self._clock.now())
        if n:
            logger.info("gcal write-back: %d block(s) re-queued for a new app calendar", n)

    async def _google[T](self, user_id: uuid.UUID, call: Awaitable[T]) -> T:
        """Token mint / app calendar: gcal domain errors → a drain outcome."""
        try:
            return await call
        except GcalGrantRevokedError:
            raise _Stop(DrainStatus.RECONNECT_REQUIRED) from None
        except GcalConnectionInactiveError:
            async with self._tx() as s:
                gone = await repo.get_connection(s, user_id) is None
            raise _Stop(
                DrainStatus.NOT_CONNECTED if gone else DrainStatus.RECONNECT_REQUIRED
            ) from None
        except GcalCredentialUndecryptableError as exc:
            logger.error("gcal write-back: stored credential undecryptable (key_id=%s)", exc.key_id)
            raise _Stop(DrainStatus.RECONNECT_REQUIRED) from None
        except GcalTransientError:
            raise _Stop(DrainStatus.UNAVAILABLE) from None


@dataclass
class _Progress:
    written: int = 0
    deleted: int = 0


# ---------------------------------------------------------------- in-process registry


class OutboxRegistry:
    """At most one drain task per user in this process, with strong references (a bare
    `create_task` result can be garbage-collected mid-flight). A kick that lands while a drain
    runs marks the user dirty, and the same task runs another pass afterwards, so work
    confirmed mid-drain is never missed. Passes are not capped: each extra one is caused by a
    real request (confirm or sync), so the loop is bounded by user activity, and a cap would
    drop the kick that landed during the last pass. Never raises into the caller."""

    def __init__(self, drain: Callable[[uuid.UUID], Awaitable[DrainResult]]) -> None:
        self._drain = drain
        self._tasks: dict[uuid.UUID, asyncio.Task[DrainResult]] = {}
        self._again: set[uuid.UUID] = set()

    def running(self, user_id: uuid.UUID) -> bool:
        task = self._tasks.get(user_id)
        return task is not None and not task.done()

    def kick(self, user_id: uuid.UUID) -> asyncio.Task[DrainResult]:
        task = self._tasks.get(user_id)
        if task is not None and not task.done():
            self._again.add(user_id)
            return task
        task = asyncio.create_task(self._run(user_id), name="gcal-writeback")
        self._tasks[user_id] = task
        task.add_done_callback(lambda t: self._forget(user_id, t))
        return task

    async def wait(self, user_id: uuid.UUID, budget_s: float) -> DrainResult | None:
        """Kick (a pass starts after this call) and wait up to `budget_s` without cancelling:
        past the budget the drain keeps going in the background and None is returned."""
        task = self.kick(user_id)
        done, _ = await asyncio.wait({task}, timeout=budget_s)
        return task.result() if done else None

    async def idle(self, budget_s: float | None = None) -> None:
        """Wait for every in-flight drain (tests, shutdown)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=budget_s)

    async def _run(self, user_id: uuid.UUID) -> DrainResult:
        result = DrainResult(DrainStatus.UNAVAILABLE)
        while True:
            self._again.discard(user_id)
            try:
                result = await self._drain(user_id)
            except Exception as exc:  # a background task must never die loudly
                logger.error("gcal write-back failed: %s", type(exc).__name__)
                result = DrainResult(DrainStatus.UNAVAILABLE)
            if user_id not in self._again:
                break
        return result

    def _forget(self, user_id: uuid.UUID, task: asyncio.Task[DrainResult]) -> None:
        if self._tasks.get(user_id) is task:
            del self._tasks[user_id]
            self._again.discard(user_id)

"""Read edits made to our events inside Google Calendar (T4.3b; ARCH §3 "Write-back outbox").

`read_google_edits` runs BEFORE the per-user lock, with no session held across a Google call
(the T4.2 lesson; see tests/api/test_busy_pool.py). It lists the app calendar over the live
plans' weeks and turns differences into `GoogleEdit`s, which
`app.db.write_paths.apply_google_edits` re-validates and applies under the lock.

Detection (`classify`, pure), per live `synced` row that has not started:
- the event's etag equals the stored one → our own write, unchanged;
- `status == cancelled`, or the event is gone (not listed, and events.get says 404/410 or
  cancelled) → the user deleted it;
- start or end differ → the user moved it, if the new span is whole minutes, starts in the
  future and lies inside the block's plan week (else ignored: a pin cannot express it, D7);
- otherwise (title/description only, or an all-day/unparseable time) → ignored; the next write
  of that block restores our title.
`pending` rows (our write in flight) and started blocks are never candidates.

A 404 on the listing means the app calendar was deleted: the live rows are re-pended FIRST
(committed), then `ensure_app_calendar(missing=id)` recreates it, the same order the write-back
uses; the caller kicks a drain to refill it. Nothing is ingested in that pass.

Privacy (CLAUDE.md rule 4): the listing asks Google only for id, etag, status, start, end and
our private extended properties (`fields=`), so summaries and descriptions are never received.
Log lines carry counts only. Nothing here reaches the LLM.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import outbox
from app.db.outbox import SyncedRow
from app.db.write_paths import GoogleEdit
from app.gcal import repository as repo
from app.gcal.calendar import CalendarNotFoundError, EventRejectedError, EventSnapshot
from app.gcal.client import GoogleUnavailableError
from app.gcal.errors import (
    GcalConnectionInactiveError,
    GcalCredentialUndecryptableError,
    GcalReconnectRequiredError,
    GcalTransientError,
    GcalUnavailableError,
)
from app.scheduler import Interval

if TYPE_CHECKING:
    from app.gcal.runtime import GcalRuntime

logger = logging.getLogger(__name__)


LOOKUP_CONCURRENCY = 5  # events.get in flight at once
MAX_LOOKUPS = 20  # events.get per sync; the rest wait for the next one


class _Failed:
    """A lookup that failed: the row stays unjudged this sync."""


_FAILED = _Failed()


@dataclass(frozen=True)
class IngestResult:
    edits: tuple[GoogleEdit, ...] = ()
    connected: bool = True  # False: the connection disappeared (disconnect) meanwhile
    recreated: bool = False  # the app calendar was gone and has been recreated (kick a drain)


def _whole_minutes(dt: datetime) -> bool:
    return dt.second == 0 and dt.microsecond == 0


def classify(
    rows: Sequence[SyncedRow], events: Mapping[str, EventSnapshot | None], now: datetime
) -> list[GoogleEdit]:
    """Rows + what Google says about their events (None: gone) → edits, ordered by the
    block's start. Pure. A row whose event id is absent from `events` is not judged."""
    edits: list[GoogleEdit] = []
    for row in rows:
        if row.event_id not in events or row.span.start <= now:
            continue
        ev = events[row.event_id]
        if ev is not None and row.etag is not None and ev.etag == row.etag:
            continue  # our own write
        if ev is not None and ev.lineage_id not in (None, str(row.lineage_id)):
            continue  # not the event we wrote for this lineage
        base = {
            "block_id": row.block_id,
            "lineage_id": row.lineage_id,
            "old_span": row.span,
            "event_id": row.event_id,
            "expected_etag": row.etag,
        }
        if ev is None or ev.status == "cancelled":
            edits.append(GoogleEdit(kind="delete", **base))  # type: ignore[arg-type]
            continue
        if ev.start is None or ev.end is None or not ev.start < ev.end:
            continue
        if ev.start == row.span.start and ev.end == row.span.end:
            continue  # title/description only
        if not (_whole_minutes(ev.start) and _whole_minutes(ev.end)):
            continue  # off the minute grid (the solver's and the pins' unit): not a pin
        new = Interval(start=ev.start, end=ev.end)
        if not (new.start > now and row.plan_horizon.contains(new)):
            continue  # into the past or out of the week: not a pin (D7)
        edits.append(
            GoogleEdit(kind="move", new_span=new, new_etag=ev.etag, **base)  # type: ignore[arg-type]
        )
    return sorted(edits, key=lambda e: (e.old_span.start, e.old_span.end, str(e.block_id)))


async def read_google_edits(
    maker: async_sessionmaker[AsyncSession],
    runtime: Callable[[], GcalRuntime],
    user_id: uuid.UUID,
    candidates: outbox.IngestCandidates,
    *,
    now: datetime,
) -> IngestResult:
    """List the app calendar and classify. Call with NO session open. Raises
    GcalReconnectRequiredError (409) / GcalUnavailableError (503), like the busy provider."""
    async with maker() as s, s.begin():
        row = await repo.get_connection(s, user_id)
    if row is None:
        return IngestResult(connected=False)
    if row.status != "active":
        raise GcalReconnectRequiredError()
    if candidates.window is None or row.app_calendar_id is None:
        return IngestResult()  # no live plan, or no calendar yet (nothing of ours in Google)
    calendar_id = row.app_calendar_id
    rt = runtime()

    async def google[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except (GcalConnectionInactiveError, GcalCredentialUndecryptableError):
            # Revoked (invalid_grant is a subclass), or no key decrypts the credential. A
            # disconnect racing this sync also lands here: one 409, the next sync is clean.
            raise GcalReconnectRequiredError() from None
        except (GcalTransientError, GoogleUnavailableError, EventRejectedError):
            raise GcalUnavailableError() from None

    token = await google(rt.service(maker).access_token(user_id))
    window = candidates.window
    try:
        listed = await google(
            rt.calendar.list_events(
                token.token, calendar_id, time_min=window.start, time_max=window.end
            )
        )
    except CalendarNotFoundError:
        logger.info("gcal ingest: app calendar gone (404); recreating")
        async with maker() as s, s.begin():
            await outbox.repend_current(s, user_id, now)
        await google(rt.app_calendar(maker).ensure_app_calendar(user_id, missing=calendar_id))
        return IngestResult(recreated=True)

    events: dict[str, EventSnapshot | None] = {ev.id: ev for ev in listed}
    # Not in the window: deleted (and purged), or moved out of the listed weeks. Looked up
    # concurrently (bounded), at most MAX_LOOKUPS per sync, nearest block first; the rest,
    # and any lookup that fails, stay unjudged until the next sync (review round 2). A failed
    # lookup never discards what the listing already showed.
    missing = [r.event_id for r in candidates.rows if r.event_id not in events]
    gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def look(event_id: str) -> tuple[str, EventSnapshot | None | _Failed]:
        async with gate:
            try:
                return event_id, await rt.calendar.get_event(token.token, calendar_id, event_id)
            except (GoogleUnavailableError, EventRejectedError):
                return event_id, _FAILED

    looked = await asyncio.gather(*(look(e) for e in missing[:MAX_LOOKUPS]))
    failed = 0
    for event_id, snapshot in looked:
        if isinstance(snapshot, _Failed):
            failed += 1
        else:
            events[event_id] = snapshot
    deferred = max(0, len(missing) - MAX_LOOKUPS)
    if failed or deferred:
        logger.info(
            "gcal ingest: %d event lookup(s) failed, %d deferred to the next sync",
            failed,
            deferred,
        )
    edits = classify(candidates.rows, events, now)
    if edits:
        moves = sum(1 for e in edits if e.kind == "move")
        logger.info(
            "gcal ingest: %d edit(s) found in Google (%d moved, %d deleted)",
            len(edits),
            moves,
            len(edits) - moves,
        )
    return IngestResult(edits=tuple(edits))

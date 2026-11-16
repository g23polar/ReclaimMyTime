"""Write-back outbox persistence (T4.3a; ARCH §3 "Write-back outbox").

The outbox is the `blocks.gcal_*` columns of LIVE plans. The worker (app.gcal.writeback)
claims work here in one short transaction, commits, calls Google with no session open, and
records each result here in another short transaction (compare-and-set). Every function takes
the per-user advisory lock first, so outbox bookkeeping serializes with confirm (which marks
changed blocks pending) without any row-lock ordering to reason about; none of them ever
opens a second session. Callers own `tx` and commit.

Event identity: one Google event per (lineage, plan week) — `event_id_for`. Lineage ids alone
repeat across weeks (pack mints `uuid5(project:ordinal:salt)`, see
solver_io.completed_minutes), so the week is part of the id; within a week a moved block keeps
its lineage and therefore its event.

Bookkeeping on non-live rows: after an orphaned event is deleted in Google, every non-live
row that named it, or whose unrecorded insert it may have been, is settled `synced` with no
event id (`record_deleted`). Those are sync columns, not plan content: spans, projects and
states are never touched here.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Block, Project, WeekPlan
from app.db.solver_io import to_interval
from app.db.write_paths import lock_user
from app.scheduler import Interval

__all__ = [
    "OutboxCounts",
    "OutboxItem",
    "claim_deletes",
    "claim_upserts",
    "counts",
    "event_id_for",
    "record_deleted",
    "record_synced",
    "repend_current",
]


def event_id_for(lineage_id: uuid.UUID, week_start: date) -> str:
    """Deterministic Google event id for a lineage in a plan week. Google accepts
    client-supplied ids of base32hex characters (0-9, a-v), 5-1024 long; hex digits and
    decimal digits are a subset, so this is 32 hex + 8 digits = 40 characters."""
    return f"{lineage_id.hex}{week_start:%Y%m%d}"


@dataclass(frozen=True)
class OutboxItem:
    """One live pending block to write. `project_name` becomes the event title: never log it."""

    block_id: uuid.UUID
    lineage_id: uuid.UUID
    week_start: date
    span: Interval
    project_name: str
    stored_event_id: str | None  # inherited from the lineage's previous row, if any

    @property
    def event_id(self) -> str:
        return event_id_for(self.lineage_id, self.week_start)

    @property
    def known_to_exist(self) -> bool:
        """An earlier version of this lineage already wrote this exact event: update first."""
        return self.stored_event_id == self.event_id


@dataclass(frozen=True)
class OutboxCounts:
    pending: int = 0
    synced: int = 0


def _live_current_plans(now: datetime):  # type: ignore[no-untyped-def]
    """Join condition: the block's plan is live and its week has not ended. Older weeks are
    history: nothing there is ever (re)written, which also bounds a first connect's backfill."""
    return and_(
        WeekPlan.id == Block.plan_id,
        WeekPlan.user_id == Block.user_id,
        WeekPlan.status == "live",
        func.upper(WeekPlan.horizon) > now,
    )


async def claim_upserts(
    tx: AsyncSession,
    user_id: uuid.UUID,
    now: datetime,
    *,
    exclude: Collection[uuid.UUID] = (),
    limit: int = 200,
) -> list[OutboxItem]:
    """Live pending blocks of the current/future weeks, nearest first. Past blocks of the
    current week are included: a pending one is written once (Principle 4 forbids REwriting
    a past block's event; only pending rows are ever claimed, so a synced one never is).
    `FOR UPDATE SKIP LOCKED` skips rows another transaction is changing right now; they are
    picked up by the next drain.

    An ended block that already has an event id is never rewritten (review round 2): only a
    first-time backfill (no event id ever recorded) may write an ended block. Such rows are
    settled here as `synced` without a Google call, keeping their id/etag."""
    await lock_user(tx, user_id)
    live_now = select(WeekPlan.id).where(
        WeekPlan.user_id == user_id, WeekPlan.status == "live", func.upper(WeekPlan.horizon) > now
    )
    await tx.execute(
        update(Block)
        .where(
            Block.user_id == user_id,
            Block.gcal_status == "pending",
            Block.gcal_event_id.is_not(None),
            func.upper(Block.span) <= now,
            Block.plan_id.in_(live_now.scalar_subquery()),
        )
        .values(gcal_status="synced")
    )
    stmt = (
        select(
            Block.id,
            Block.lineage_id,
            WeekPlan.week_start,
            Block.span,
            Project.name,
            Block.gcal_event_id,
        )
        .join(WeekPlan, _live_current_plans(now))
        .join(Project, and_(Project.id == Block.project_id, Project.user_id == Block.user_id))
        .where(Block.user_id == user_id, Block.gcal_status == "pending")
        .order_by(func.lower(Block.span), Block.id)
        .limit(limit)
        .with_for_update(of=Block, skip_locked=True)
    )
    if exclude:
        stmt = stmt.where(Block.id.not_in(sorted(exclude)))
    return [
        OutboxItem(
            block_id=bid,
            lineage_id=lid,
            week_start=week,
            span=to_interval(span),
            project_name=name,
            stored_event_id=stored,
        )
        for bid, lid, week, span, name, stored in (await tx.execute(stmt)).all()
    ]


async def claim_deletes(
    tx: AsyncSession, user_id: uuid.UUID, now: datetime, *, limit: int = 200
) -> list[str]:
    """Event ids that may exist in Google for a lineage-week no longer in a live plan (the
    block was removed, or superseded with no successor). Two sources, both limited to
    blocks that have not ended (past events are never touched):

    - recorded: a non-live row names an event id (`gcal_event_id`);
    - unrecorded (review round 1): a `superseded` row still `pending` with no stored id. Only
      live rows are ever claimed, and only a live plan becomes superseded, so such a row may
      have been inserted at Google with the result never recorded (crash, failed CAS
      transaction). Its event, if any, has the deterministic id, which is reconstructed;
      deleting a non-existent event answers 404/410, which counts as done.

    An id still named by a live row (derived from its lineage, or stored on it) is never an
    orphan. `record_deleted` makes both kinds stop being candidates."""
    await lock_user(tx, user_id)
    live = (
        await tx.execute(
            select(Block.lineage_id, WeekPlan.week_start, Block.gcal_event_id)
            .join(WeekPlan, _live_current_plans(now))
            .where(Block.user_id == user_id)
        )
    ).all()
    wanted = {event_id_for(lid, week) for lid, week, _ in live} | {
        stored for _, _, stored in live if stored is not None
    }
    recorded = (
        await tx.execute(
            select(Block.gcal_event_id)
            .join(
                WeekPlan,
                and_(
                    WeekPlan.id == Block.plan_id,
                    WeekPlan.user_id == Block.user_id,
                    WeekPlan.status != "live",
                ),
            )
            .where(
                Block.user_id == user_id,
                Block.gcal_event_id.is_not(None),
                func.upper(Block.span) > now,
            )
            .distinct()
        )
    ).scalars()
    unrecorded = (
        await tx.execute(
            select(Block.lineage_id, WeekPlan.week_start)
            .join(WeekPlan, _superseded_plans())
            .where(
                Block.user_id == user_id,
                Block.gcal_status == "pending",
                Block.gcal_event_id.is_(None),
                func.upper(Block.span) > now,
            )
            .distinct()
        )
    ).all()
    candidates = set(recorded) | {event_id_for(lid, week) for lid, week in unrecorded}
    return sorted(e for e in candidates if e not in wanted)[:limit]


def _superseded_plans():  # type: ignore[no-untyped-def]
    return and_(
        WeekPlan.id == Block.plan_id,
        WeekPlan.user_id == Block.user_id,
        WeekPlan.status == "superseded",
    )


def _lineage_week(event_id: str) -> tuple[uuid.UUID, date] | None:
    """Inverse of `event_id_for` (None for an id of another shape)."""
    if len(event_id) != 40:
        return None
    try:
        return uuid.UUID(hex=event_id[:32]), datetime.strptime(event_id[32:], "%Y%m%d").date()
    except ValueError:
        return None


async def record_synced(
    tx: AsyncSession,
    user_id: uuid.UUID,
    block_id: uuid.UUID,
    event_id: str,
    etag: str,
    title: str,
) -> bool:
    """CAS pending → synced on exactly the row that was written. The row may have been
    superseded meanwhile; it is still recorded (the event exists), and its live successor,
    which inherited the pre-write `pending`, is written again by the next pass.

    `title` is the summary that was written. If the project was renamed while the Google
    call was in flight (review round 2), the row stays `pending` but stores the new event
    id/etag, so the next pass updates the event with the current name. Returns True only
    when the row became `synced`. Same transaction as the CAS, under the user lock, which
    the rename also takes."""
    await lock_user(tx, user_id)
    current = (
        await tx.execute(
            select(Project.name)
            .join(Block, and_(Block.project_id == Project.id, Block.user_id == Project.user_id))
            .where(Block.id == block_id, Block.user_id == user_id)
        )
    ).scalar_one_or_none()
    status = "synced" if current == title else "pending"
    result = await tx.execute(
        update(Block)
        .where(Block.id == block_id, Block.user_id == user_id, Block.gcal_status == "pending")
        .values(gcal_status=status, gcal_event_id=event_id, gcal_etag=etag)
    )
    return bool(result.rowcount) and status == "synced"  # type: ignore[attr-defined]


async def record_deleted(tx: AsyncSession, user_id: uuid.UUID, event_id: str) -> int:
    """The event is gone in Google: non-live rows that named it stop naming it, and
    superseded rows whose unrecorded insert it may have been (pending, no stored id, same
    lineage-week) are settled. Both end `synced` with no event id, meaning "nothing in
    Google for this row", so `claim_deletes` never offers them again. Only sync columns
    change. Live rows are left alone (one may have re-acquired the lineage-week
    concurrently; its own write recreates the event)."""
    await lock_user(tx, user_id)
    non_live = select(WeekPlan.id).where(WeekPlan.user_id == user_id, WeekPlan.status != "live")
    settled = {"gcal_status": "synced", "gcal_event_id": None, "gcal_etag": None}
    result = await tx.execute(
        update(Block)
        .where(
            Block.user_id == user_id,
            Block.gcal_event_id == event_id,
            Block.plan_id.in_(non_live.scalar_subquery()),
        )
        .values(**settled)
    )
    n = int(result.rowcount)  # type: ignore[attr-defined]
    key = _lineage_week(event_id)
    if key is not None:
        lineage_id, week_start = key
        superseded = select(WeekPlan.id).where(
            WeekPlan.user_id == user_id,
            WeekPlan.status == "superseded",
            WeekPlan.week_start == week_start,
        )
        result = await tx.execute(
            update(Block)
            .where(
                Block.user_id == user_id,
                Block.lineage_id == lineage_id,
                Block.gcal_status == "pending",
                Block.gcal_event_id.is_(None),
                Block.plan_id.in_(superseded.scalar_subquery()),
            )
            .values(**settled)
        )
        n += int(result.rowcount)  # type: ignore[attr-defined]
    return n


async def repend_current(tx: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """The app calendar was (re)created: its events are gone, so every live block that has not
    ended is written again. Ended blocks are not (Principle 4)."""
    await lock_user(tx, user_id)
    live = select(WeekPlan.id).where(
        WeekPlan.user_id == user_id, WeekPlan.status == "live", func.upper(WeekPlan.horizon) > now
    )
    result = await tx.execute(
        update(Block)
        .where(
            Block.user_id == user_id,
            Block.gcal_status == "synced",
            func.upper(Block.span) > now,
            Block.plan_id.in_(live.scalar_subquery()),
        )
        .values(gcal_status="pending")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def counts(tx: AsyncSession, user_id: uuid.UUID, now: datetime) -> OutboxCounts:
    """Outbox state of the live plans of the current/future weeks (no lock: a read)."""
    rows = (
        await tx.execute(
            select(Block.gcal_status, func.count())
            .join(WeekPlan, _live_current_plans(now))
            .where(Block.user_id == user_id)
            .group_by(Block.gcal_status)
        )
    ).all()
    by_status = {str(status): int(n) for status, n in rows}
    return OutboxCounts(pending=by_status.get("pending", 0), synced=by_status.get("synced", 0))

"""Changesets (`changesets` tag): confirm / reject, both idempotent on their own terminal state.

Confirm reads fresh calendar busy time first (T4.2), outside any transaction or lock: a
transient Google failure is 503 `gcal_unavailable`, a revoked grant 409
`gcal_reconnect_required`, nothing applied. A future proposed block overlapping fresh busy
time is O8-stale (see `confirm_changeset`).

Confirm owns its transactions: an O8 `ChangesetStale` rolls the confirm back, then a NEW
transaction stages the fresh re-solve via `stage_changeset` (expiring the stale changeset)
and the client gets 409 `changeset_stale` with `details.changeset_id`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.busy import Busy, BusyWindow, fetch_window
from app.api.deps import Maker, Now, UserId, transaction
from app.api.outbox import kick_outbox
from app.api.views import changeset_view
from app.auth.dependencies import require_timezone
from app.db.models import Changeset, WeekPlan
from app.db.models.staged import parse_staged
from app.db.solver_io import to_interval
from app.db.write_paths import (
    ChangesetNotPending,
    ChangesetStale,
    InvalidMutation,
    confirm_changeset,
    lock_user,
    propose_changeset,
    reject_changeset,
    solve_horizon,
    stage_changeset,
)
from app.errors import AppError
from app.gcal.busy import BusyProvider
from app.scheduler import Interval

router = APIRouter(tags=["changesets"], dependencies=[Depends(require_timezone)])


@router.post("/changesets/{changeset_id}/confirm", operation_id="confirmChangeset")
async def confirm(
    request: Request,
    user_id: UserId,
    maker: Maker,
    now: Now,
    busy: Busy,
    changeset_id: uuid.UUID,
) -> dict[str, Any]:
    # T4.2: fresh busy time, read before the confirm transaction and its advisory lock, with
    # no pooled connection held across the Google call (review round 1).
    fresh = await _fresh_busy(maker, user_id, changeset_id, now=now, provider=busy)
    try:
        async with transaction(maker) as tx:
            busy_now = None if fresh is None else fresh.intervals
            await confirm_changeset(tx, user_id, changeset_id, now=now, busy=busy_now)
            body = await changeset_view(tx, user_id, changeset_id)
        # Committed: drain the write-back outbox in the background (T4.3a). Fire-and-forget,
        # so it never delays or fails this response; leftovers wait for the next kick/sync.
        kick_outbox(request.app, user_id)
        return body
    except ChangesetStale as stale:  # the confirm transaction has rolled back
        async with transaction(maker) as tx:
            outcome = await _restage(
                tx,
                user_id,
                changeset_id,
                stale,
                now=now,
                busy=() if fresh is None else fresh.intervals,
            )
        if isinstance(outcome, dict):  # confirmed by a concurrent request: idempotent 200
            kick_outbox(request.app, user_id)
            return outcome
        raise AppError(
            409,
            "changeset_stale",
            "This proposal is out of date; a fresh one was staged for review.",
            {"changeset_id": str(outcome)},
        ) from None


async def _fresh_busy(
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    changeset_id: uuid.UUID,
    *,
    now: datetime,
    provider: BusyProvider,
) -> BusyWindow | None:
    """Busy time for confirming a pending, planned changeset; None when confirm will not
    solve (unknown id, not pending, onboarding changeset without a plan). The window covers
    the proposal's week and the current preference week (they differ after a rollover, when
    confirm re-solves over the current week). Fails closed (503/409) before anything runs."""
    async with transaction(maker) as tx:
        row = (
            await tx.execute(
                select(Changeset.status, Changeset.staged, WeekPlan.horizon)
                .join(WeekPlan, WeekPlan.id == Changeset.plan_id)
                .where(Changeset.id == changeset_id, Changeset.user_id == user_id)
            )
        ).one_or_none()
        if row is None or row.status != "pending":
            return None
        window = to_interval(row.horizon)
        current = await solve_horizon(tx, user_id, parse_staged(row.staged), now)
    if current is not None and current != window:
        window = Interval(start=min(window.start, current.start), end=max(window.end, current.end))
    return await fetch_window(provider, user_id, window)


async def _restage(
    tx: AsyncSession,
    user_id: uuid.UUID,
    changeset_id: uuid.UUID,
    stale: ChangesetStale,
    *,
    now: datetime,
    busy: tuple[Interval, ...],
) -> uuid.UUID | dict[str, Any]:
    """Stage the fresh re-solve, but only if the stale changeset is still the user's current
    pending one. Between the rollback and this transaction another request may have rejected
    it, or staged a newer proposal (which expired it): re-staging then would revive rejected
    changes or expire the newer proposal (deleting its staged projects). Eligibility is
    re-checked under the per-user lock, which every write path also takes. `busy` is the
    fresh busy time read before confirm (T4.2)."""
    await lock_user(tx, user_id)
    rows = (
        await tx.execute(
            select(Changeset.id, Changeset.status, Changeset.staged, Changeset.reason).where(
                Changeset.user_id == user_id,
                or_(Changeset.id == changeset_id, Changeset.status == "pending"),
            )
        )
    ).all()
    mine = next(r for r in rows if r.id == changeset_id)
    if mine.status == "confirmed":
        return await changeset_view(tx, user_id, changeset_id)
    if mine.status != "pending":
        raise ChangesetNotPending(mine.status)
    if any(r.id != changeset_id for r in rows):
        # D4 makes this unreachable (staging expires the older pending one under the lock);
        # refuse rather than expire someone else's proposal.
        raise ChangesetNotPending("expired")
    staged = parse_staged(mine.staged)
    source = staged[0].source if staged else "system"
    reason = mine.reason  # the fresh proposal answers the same question (T4.3 contract)
    try:
        async with tx.begin_nested():
            return await stage_changeset(
                tx,
                user_id,
                staged,
                stale.fresh_input,
                stale.fresh_output,
                now=now,
                source=source,
                reason=reason,
            )
    except InvalidMutation:
        # The stale proposal's week is no longer the current one (or the live plan moved):
        # solve the same staged list afresh over the current week instead.
        return await propose_changeset(
            tx, user_id, staged, now=now, busy=busy, source=source, reason=reason
        )


@router.post("/changesets/{changeset_id}/reject", operation_id="rejectChangeset")
async def reject(
    request: Request, user_id: UserId, maker: Maker, now: Now, changeset_id: uuid.UUID
) -> dict[str, Any]:
    async with transaction(maker) as tx:
        repended = await reject_changeset(tx, user_id, changeset_id, now=now)
        body = await changeset_view(tx, user_id, changeset_id)
    if repended:
        # A rejected `google_edit` (T4.3b): write our planned times back over the user's
        # Google edits, after commit, in the background.
        kick_outbox(request.app, user_id)
    return body

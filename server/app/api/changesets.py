"""Changesets (`changesets` tag): confirm / reject, both idempotent on their own terminal state.

Confirm owns its transactions: an O8 `ChangesetStale` rolls the confirm back, then a NEW
transaction stages the fresh re-solve via `stage_changeset` (expiring the stale changeset)
and the client gets 409 `changeset_stale` with `details.changeset_id`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Maker, Now, Tx, UserId, transaction
from app.api.views import changeset_view
from app.auth.dependencies import require_timezone
from app.db.models import Changeset
from app.db.models.staged import parse_staged
from app.db.write_paths import (
    ChangesetNotPending,
    ChangesetStale,
    InvalidMutation,
    confirm_changeset,
    lock_user,
    propose_changeset,
    reject_changeset,
    stage_changeset,
)
from app.errors import AppError

router = APIRouter(tags=["changesets"], dependencies=[Depends(require_timezone)])


@router.post("/changesets/{changeset_id}/confirm", operation_id="confirmChangeset")
async def confirm(
    user_id: UserId, maker: Maker, now: Now, changeset_id: uuid.UUID
) -> dict[str, Any]:
    try:
        async with transaction(maker) as tx:
            # TODO(T4.2): pass fresh freeBusy as `busy`; None reuses the proposal's.
            await confirm_changeset(tx, user_id, changeset_id, now=now)
            return await changeset_view(tx, user_id, changeset_id)
    except ChangesetStale as stale:  # the confirm transaction has rolled back
        async with transaction(maker) as tx:
            outcome = await _restage(tx, user_id, changeset_id, stale, now=now)
            if isinstance(outcome, dict):  # confirmed by a concurrent request: idempotent 200
                return outcome
        raise AppError(
            409,
            "changeset_stale",
            "This proposal is out of date; a fresh one was staged for review.",
            {"changeset_id": str(outcome)},
        ) from None


async def _restage(
    tx: AsyncSession,
    user_id: uuid.UUID,
    changeset_id: uuid.UUID,
    stale: ChangesetStale,
    *,
    now: datetime,
) -> uuid.UUID | dict[str, Any]:
    """Stage the fresh re-solve, but only if the stale changeset is still the user's current
    pending one. Between the rollback and this transaction another request may have rejected
    it, or staged a newer proposal (which expired it): re-staging then would revive rejected
    changes or expire the newer proposal (deleting its staged projects). Eligibility is
    re-checked under the per-user lock, which every write path also takes."""
    await lock_user(tx, user_id)
    rows = (
        await tx.execute(
            select(Changeset.id, Changeset.status, Changeset.staged).where(
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
    try:
        async with tx.begin_nested():
            return await stage_changeset(
                tx, user_id, staged, stale.fresh_input, stale.fresh_output, now=now, source=source
            )
    except InvalidMutation:
        # The stale proposal's week is no longer the current one (or the live plan moved):
        # solve the same staged list afresh over the current week instead.
        return await propose_changeset(tx, user_id, staged, now=now, source=source)


@router.post("/changesets/{changeset_id}/reject", operation_id="rejectChangeset")
async def reject(user_id: UserId, tx: Tx, now: Now, changeset_id: uuid.UUID) -> dict[str, Any]:
    await reject_changeset(tx, user_id, changeset_id, now=now)
    return await changeset_view(tx, user_id, changeset_id)

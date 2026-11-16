"""Per-turn context loading (one short read transaction, before any model call).

Builds the `TurnStaging` (labels + pending changeset) and the snapshot data. Label order is
creation order, so a user's labels are stable from turn to turn unless rows come or go.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.narration import project_names
from app.agent.prompt import ExceptionInfo, LiveBlock, PinInfo, SnapshotData
from app.agent.staging import TurnInputs, TurnStaging
from app.db.models import (
    AvailabilityException,
    Block,
    BlockPin,
    Changeset,
    Project,
    UserTag,
    WeekPlan,
)
from app.db.models.staged import InvalidMutation, parse_staged
from app.db.solver_io import completed_minutes, load_constraint_state, to_interval
from app.db.write_paths import sweep_past_blocks

log = logging.getLogger(__name__)

_LIVE_BLOCKS_SHOWN = 60  # more than a dense week; keeps the snapshot bounded


@dataclass(frozen=True)
class TurnContext:
    staging: TurnStaging
    snapshot: SnapshotData
    committed_names: dict[str, str]
    pending_id: uuid.UUID | None  # the pending changeset this turn builds on, if any


async def load_turn(
    tx: AsyncSession, user_id: uuid.UUID, *, now: datetime, device_tz: str
) -> TurnContext:
    await sweep_past_blocks(tx, user_id, now)  # completed minutes must be current
    committed = await load_constraint_state(tx, user_id)
    timezone = committed.preferences.timezone if committed.preferences else device_tz

    projects = (
        await tx.execute(
            select(Project.id, Project.name)
            .where(Project.user_id == user_id, Project.status.in_(("active", "paused", "done")))
            .order_by(Project.created_at, Project.id)
        )
    ).all()
    names = {str(pid): name for pid, name in projects}

    exceptions = [
        ExceptionInfo(
            id=str(r.id),
            span=to_interval(r.span),
            blocks_all=r.blocks_all,
            tags=tuple(r.blocks_tags),
            reason=r.reason,
        )
        for r in (
            await tx.execute(
                select(AvailabilityException)
                .where(
                    AvailabilityException.user_id == user_id,
                    func.upper(AvailabilityException.span) > now,
                )
                .order_by(AvailabilityException.created_at, AvailabilityException.id)
            )
        ).scalars()
    ]
    pins = [
        PinInfo(
            id=str(r.id),
            kind=r.kind,
            span=to_interval(r.span),
            project_id=None if r.project_id is None else str(r.project_id),
        )
        for r in (
            await tx.execute(
                select(BlockPin)
                .where(BlockPin.user_id == user_id, func.upper(BlockPin.span) > now)
                .order_by(BlockPin.created_at, BlockPin.id)
            )
        ).scalars()
    ]
    vocabulary = list(
        (
            await tx.execute(
                select(UserTag.tag).where(UserTag.user_id == user_id).order_by(UserTag.tag)
            )
        ).scalars()
    )
    pending = (
        await tx.execute(
            select(Changeset)
            .where(Changeset.user_id == user_id, Changeset.status == "pending")
            .order_by(Changeset.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    live = [
        LiveBlock(project_id=str(b.project_id), span=to_interval(b.span), state=b.state)
        for b in (
            await tx.execute(
                select(Block)
                .join(WeekPlan, WeekPlan.id == Block.plan_id)
                .where(
                    WeekPlan.user_id == user_id,
                    WeekPlan.status == "live",
                    func.upper(Block.span) > now,
                )
                .order_by(func.lower(Block.span), Block.lineage_id)
                .limit(_LIVE_BLOCKS_SHOWN)
            )
        ).scalars()
    ]

    def inputs(pending_staged: list) -> TurnInputs:
        return TurnInputs(
            committed=committed,
            project_ids=list(names),
            exception_ids=[e.id for e in exceptions],
            pin_ids=[p.id for p in pins],
            vocabulary=vocabulary,
            pending=pending_staged,
            timezone=timezone,
        )

    pending_id = None if pending is None else pending.id
    try:
        staging = TurnStaging(inputs(parse_staged(pending.staged) if pending else []))
    except (InvalidMutation, ValidationError):
        # The pending list no longer applies to committed state (rows changed underneath
        # it); confirm would refuse it anyway. Start clean; a new proposal expires it. Keep
        # its id: it is still the pending row this turn was loaded against (stale check).
        log.warning("pending changeset %s no longer applies; turn starts clean", pending_id)
        staging = TurnStaging(inputs([]))

    # One effective timezone for the snapshot clock and for converting tool inputs: a
    # pending preferences change may carry a new one (TurnStaging uses the overlay too).
    effective = staging.state().preferences
    timezone = effective.timezone if effective is not None else timezone

    snapshot = SnapshotData(
        now=now,
        timezone=timezone,
        names=project_names(staging.mutations, names),
        exceptions={e.id: e for e in exceptions},
        pins={p.id: p for p in pins},
        completed_minutes=await completed_minutes(tx, user_id),
        live_blocks=live,
        # Deliberately empty (T4.2): busy time reaches the solver, not the model. Filling it
        # would put a freeBusy call before every turn, chit-chat included, and let plain
        # chat fail on a Google outage; extraction doesn't need it.
        busy=(),
    )
    return TurnContext(
        staging=staging, snapshot=snapshot, committed_names=names, pending_id=pending_id
    )

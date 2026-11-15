"""The only write entry points for scheduling state and plans (ARCHITECTURE.md §3 Semantics;
CLAUDE.md rules 1, 2, 3, 5).

- `persist_plan` is the sole creator of `week_plans` + `blocks`, and refuses any plan that
  `scheduler.verify()` rejects.
- `apply_mutation` is the sole mutator of projects / preferences / exceptions / pins /
  user_tags, and writes the `mutations` trace row in the same transaction.
- `stage_changeset` / `confirm_changeset` / `reject_changeset` implement propose → confirm.
- `sweep_past_blocks` / `mark_block_state` are the only block-state writes.

Transaction discipline (same as app/auth/repository.py): the caller owns `tx` and commits;
nothing here commits or rolls back. A raised exception means the caller must roll back.
Every function takes the per-user advisory lock (re-entrant within a transaction), with the
exact key auth uses. Nothing reads the clock: `now` is injected and also stamps every
`created_at` / `resolved_at` / `superseded_at` written here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, bindparam, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.db.models import (
    AvailabilityException,
    Block,
    BlockPin,
    Changeset,
    Mutation,
    Project,
    UserPreferences,
    UserTag,
    WeekPlan,
)
from app.db.models.staged import (
    ExceptionAdded,
    ExceptionRemoved,
    InvalidMutation,
    PinAdded,
    PinRemoved,
    PreferencesUpdated,
    ProjectArchived,
    ProjectCreated,
    ProjectUpdated,
    StagedMutation,
    created_project_ids,
    dump_staged,
    parse_staged,
)
from app.db.solver_io import (
    PreferencesNotSet,
    SourceBlock,
    block_rows,
    build_solver_input,
    load_constraint_state,
    load_previous,
    overlay,
    to_interval,
    to_range,
    week_horizon,
    week_start_for,
)
from app.scheduler import Interval, PlannedBlock, SolverInput, SolverOutput, Violation, pack, verify

__all__ = [
    "BlockNotFound",
    "BlockNotStarted",
    "BlockSuperseded",
    "ChangesetNotFound",
    "ChangesetNotPending",
    "ChangesetStale",
    "ConfirmResult",
    "InvalidMutation",
    "PlanVerificationError",
    "PreferencesNotSet",
    "WritePathError",
    "apply_mutation",
    "carry_forward",
    "confirm_changeset",
    "expected_horizon",
    "lock_user",
    "mark_block_state",
    "persist_plan",
    "propose_changeset",
    "reject_changeset",
    "stage_changeset",
    "sweep_past_blocks",
    "user_lock_statement",
]


# ---------------------------------------------------------------- errors


class WritePathError(Exception):
    """Base for typed write-path refusals (T2.3 maps each to an HTTP error)."""


class ChangesetNotFound(WritePathError):
    """Unknown id, or another user's changeset (→ 404 not_found)."""


class ChangesetNotPending(WritePathError):
    """Confirm/reject on a changeset in another terminal state (→ 409 changeset_not_pending)."""

    def __init__(self, status: str) -> None:
        super().__init__(f"changeset is {status}, not pending")
        self.status = status


class ChangesetStale(WritePathError):
    """O8: re-solving on current state no longer reproduces the stored proposal. Nothing was
    applied. `fresh_input`/`fresh_output` are the re-solve, for staging a replacement
    changeset in a NEW transaction (this one must roll back) → 409 changeset_stale."""

    def __init__(self, fresh_input: SolverInput, fresh_output: SolverOutput, reason: str) -> None:
        super().__init__(f"changeset is stale: {reason}")
        self.fresh_input = fresh_input
        self.fresh_output = fresh_output
        self.reason = reason


class PlanVerificationError(WritePathError):
    """`persist_plan` refused a plan: verify() reported violations (a solver bug, not a
    user error → 500)."""

    def __init__(self, violations: Sequence[Violation], message: str | None = None) -> None:
        super().__init__(message or f"plan failed verify(): {[v.kind for v in violations]}")
        self.violations = list(violations)


class BlockNotFound(WritePathError):
    """Unknown block id or another user's block (→ 404 not_found)."""


class BlockSuperseded(WritePathError):
    """Block is not in a live plan (→ 409 block_superseded)."""


class BlockNotStarted(WritePathError):
    """complete/skip on a block that has not started (→ 409 block_not_started)."""


# ---------------------------------------------------------------- lock


def user_lock_statement(user_id: uuid.UUID) -> TextClause:
    # ARCH §3 Semantics, fixed key; byte-identical to app.auth.repository.user_lock_statement
    # (asserted in tests/db/test_lock_key.py). Not imported from app.auth to keep db → auth
    # dependency-free.
    return text("SELECT pg_advisory_xact_lock(hashtext(CAST(:user_id AS uuid)::text))").bindparams(
        bindparam("user_id", user_id, type_=PG_UUID(as_uuid=True))
    )


async def lock_user(tx: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize every changeset/plan write for `user_id` until `tx` ends (re-entrant)."""
    await tx.execute(user_lock_statement(user_id))


# ---------------------------------------------------------------- horizon rules


def expected_horizon(now: datetime, timezone: str, week_start: int) -> Interval:
    """Plans are per user-week: the only valid horizon is the preference week holding `now`."""
    return week_horizon(week_start_for(now, timezone, week_start), timezone)


def _check_horizon(horizon: Interval, now: datetime, timezone: str, week_start: int) -> None:
    expected = expected_horizon(now, timezone, week_start)
    if horizon != expected:
        raise InvalidMutation(
            "validation_failed",
            f"plan horizon [{horizon.start.isoformat()}, {horizon.end.isoformat()}) is not the"
            f" current week [{expected.start.isoformat()}, {expected.end.isoformat()})",
        )


async def _check_supersedable(
    tx: AsyncSession, user_id: uuid.UUID, horizon: Interval, now: datetime
) -> list[uuid.UUID]:
    """The live plans a promotion over `horizon` will supersede (every overlapping one — the
    exclusion constraint leaves no choice). An identical horizon is always fine. A different
    one (a week-start change) is accepted only if none of its blocks outside `horizon` is
    still to come: past effort stays credited via superseded plans (`completed_minutes`),
    but a future/in-progress block outside the reviewed proposal would be silently dropped,
    so that is refused with InvalidMutation('validation_failed')."""
    plans = (
        await tx.execute(
            select(WeekPlan.id, WeekPlan.horizon).where(
                WeekPlan.user_id == user_id,
                WeekPlan.status == "live",
                WeekPlan.horizon.overlaps(to_range(horizon)),
            )
        )
    ).all()
    differing = [pid for pid, h in plans if to_interval(h) != horizon]
    if differing:
        lost = (
            await tx.execute(
                select(func.count())
                .select_from(Block)
                .where(
                    Block.plan_id.in_(sorted(differing)),
                    ~Block.span.contained_by(to_range(horizon)),
                    func.upper(Block.span) > now,
                )
            )
        ).scalar_one()
        if lost:
            raise InvalidMutation(
                "validation_failed",
                f"{lost} upcoming block(s) of the live plan fall outside the new week; promoting"
                " would drop them (week start moved backwards mid-week)",
            )
    return sorted(pid for pid, _ in plans)


async def _preference_week(
    tx: AsyncSession, user_id: uuid.UUID, staged: Sequence[StagedMutation] = ()
) -> tuple[str, int] | None:
    prefs = overlay(await load_constraint_state(tx, user_id), staged).preferences
    return None if prefs is None else (prefs.timezone, prefs.week_start)


# ---------------------------------------------------------------- persist_plan


def _week_start(horizon: Interval, timezone: str) -> Any:
    return horizon.start.astimezone(ZoneInfo(timezone)).date()


async def persist_plan(
    tx: AsyncSession,
    user_id: uuid.UUID,
    solver_input: SolverInput,
    solver_output: SolverOutput,
    *,
    now: datetime,
    timezone: str,
    status: Literal["proposed", "live"] = "proposed",
    week_start: int | None = None,
) -> uuid.UUID:
    """Create a `week_plans` row + its `blocks` — the ONLY code path that creates either.

    Refuses (PlanVerificationError) unless `verify(solver_output.blocks, solver_input) == []`
    and both carry the same solver/schema versions. Stores the SolverInput JSON so the solve
    is replayable. Frozen blocks are copied forward verbatim with their lineage (and gcal
    outbox fields from the live plan's row of that lineage); new blocks get
    `gcal_status='pending'`. `status='live'` requires the caller to have superseded any
    overlapping live plan first (the exclusion constraint enforces it).

    The horizon must be the preference week containing `now` (`expected_horizon`), else
    InvalidMutation('validation_failed'). `week_start` defaults to the committed
    preferences' value; `stage_changeset` passes the overlaid one.
    """
    await lock_user(tx, user_id)
    if week_start is None:
        week = await _preference_week(tx, user_id)
        if week is None:
            raise PreferencesNotSet("no preferences: cannot store a plan")
        week_start = week[1]
    _check_horizon(solver_input.horizon, now, timezone, week_start)
    if (solver_input.solver_version, solver_input.input_schema_version) != (
        solver_output.solver_version,
        solver_output.input_schema_version,
    ):
        raise PlanVerificationError([], "solver_input and solver_output versions differ")
    violations = verify(list(solver_output.blocks), solver_input)
    if violations:
        raise PlanVerificationError(violations)

    week_start = _week_start(solver_input.horizon, timezone)
    version = (
        await tx.execute(
            select(func.coalesce(func.max(WeekPlan.version), 0) + 1).where(
                WeekPlan.user_id == user_id, WeekPlan.week_start == week_start
            )
        )
    ).scalar_one()
    plan_id = uuid.uuid4()
    await tx.execute(
        insert(WeekPlan).values(
            id=plan_id,
            user_id=user_id,
            horizon=to_range(solver_input.horizon),
            week_start=week_start,
            timezone=timezone,
            version=version,
            status=status,
            solver_input=solver_input.model_dump(mode="json"),
            infeasibility=(
                None
                if solver_output.infeasibility is None
                else solver_output.infeasibility.model_dump(mode="json")
            ),
            created_at=now,
        )
    )
    sources = await _live_sources(tx, user_id, solver_input)
    rows = block_rows(plan_id, user_id, solver_input, solver_output.blocks, sources)
    if rows:
        await tx.execute(insert(Block), rows)
    return plan_id


async def _live_sources(
    tx: AsyncSession, user_id: uuid.UUID, solver_input: SolverInput
) -> dict[str, SourceBlock]:
    """Outbox fields of each frozen block's live row, matched on (lineage, span) within the
    live plan(s) over the horizon: lineage ids alone can repeat across weeks."""
    if not solver_input.frozen:
        return {}
    ids = sorted({uuid.UUID(f.lineage_id) for f in solver_input.frozen})
    rows = await tx.execute(
        select(
            Block.lineage_id,
            Block.span,
            Block.state,
            Block.gcal_status,
            Block.gcal_event_id,
            Block.gcal_etag,
        )
        .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
        .where(
            Block.user_id == user_id,
            WeekPlan.status == "live",
            WeekPlan.horizon.overlaps(to_range(solver_input.horizon)),
            Block.lineage_id.in_(ids),
        )
    )
    by_key = {
        (str(lid), to_interval(sp)): SourceBlock(
            state=st, gcal_status=gs, gcal_event_id=ev, gcal_etag=et
        )
        for lid, sp, st, gs, ev, et in rows.all()
    }
    return {
        f.lineage_id: by_key[(f.lineage_id, f.span)]
        for f in solver_input.frozen
        if (f.lineage_id, f.span) in by_key
    }


# ---------------------------------------------------------------- apply_mutation


def _project_snapshot(p: Project) -> dict[str, Any]:
    def num(v: Decimal | None) -> float | None:
        return None if v is None else float(v)

    return {
        "name": p.name,
        "kind": p.kind,
        "target_date": None if p.target_date is None else p.target_date.isoformat(),
        "estimated_total_hours": num(p.estimated_total_hours),
        "weekly_target_hours": num(p.weekly_target_hours),
        "max_block_minutes": p.max_block_minutes,
        "min_block_minutes": p.min_block_minutes,
        "preferred_windows": p.preferred_windows,
        "preferred_strict": p.preferred_strict,
        "requirements": list(p.requirements),
        "status": p.status,
    }


def _span_json(iv: Interval) -> dict[str, str]:
    return {"start": iv.start.isoformat(), "end": iv.end.isoformat()}


async def _get_project(
    tx: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> Project | None:
    return (
        await tx.execute(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _add_tags(tx: AsyncSession, user_id: uuid.UUID, tags: Sequence[str]) -> None:
    # contracts O3 (assumed): a tag first used by a confirmed write joins the vocabulary.
    if tags:
        await tx.execute(
            pg_insert(UserTag)
            .values([{"user_id": user_id, "tag": t} for t in sorted(set(tags))])
            .on_conflict_do_nothing()
        )


def _project_values(m: ProjectCreated) -> dict[str, Any]:
    d = m.payload.project
    return {
        "name": d.name,
        "kind": d.kind,
        "target_date": getattr(d, "target_date", None),
        "estimated_total_hours": _decimal(getattr(d, "estimated_total_hours", None)),
        "weekly_target_hours": _decimal(getattr(d, "weekly_target_hours", None)),
        "max_block_minutes": d.max_block_minutes,
        "min_block_minutes": d.effective_min_block_minutes,
        "preferred_windows": d.preferred_windows,
        "preferred_strict": d.preferred_strict,
        "requirements": list(d.requirements),
    }


def _decimal(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def apply_mutation(
    tx: AsyncSession,
    user_id: uuid.UUID,
    mutation: StagedMutation,
    *,
    now: datetime,
    changeset_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Apply one constraint mutation and write its `mutations` trace row (same transaction).
    Returns the trace row id. `source` and `interpretation` travel inside the mutation (the
    contract's StagedMutation shape), so the trace cannot disagree with what was staged.

    `project_created` on a row that is still `staged` (O1) completes it: values from the
    payload, status → `active`. Raises InvalidMutation for anything the current state
    cannot take (unknown/foreign id, archived project, kind mismatch, duplicate id).
    """
    await lock_user(tx, user_id)
    before: dict[str, Any] | None = None

    if isinstance(mutation, ProjectCreated):
        pid = mutation.payload.project_id
        row = await _get_project(tx, user_id, pid)
        values = _project_values(mutation)
        if row is None:
            # A foreign user's row with this id would surface as a PK IntegrityError.
            await tx.execute(
                insert(Project).values(
                    id=pid, user_id=user_id, status="active", created_at=now, **values
                )
            )
        elif row.status == "staged":
            await tx.execute(
                update(Project)
                .where(Project.id == pid, Project.user_id == user_id)
                .values(status="active", **values)
            )
        else:
            raise InvalidMutation("project_exists", f"project {pid} already exists")
        await _add_tags(tx, user_id, mutation.payload.project.requirements)

    elif isinstance(mutation, ProjectUpdated):
        pid = mutation.payload.project_id
        row = await _get_project(tx, user_id, pid)
        if row is None or row.status == "staged":
            raise InvalidMutation("not_found", f"project {pid} not found")
        if row.status == "archived":
            raise InvalidMutation("project_archived", f"project {pid} is archived")
        before = _project_snapshot(row)
        sent = mutation.payload.changes.sent()
        wrong = (
            {"weekly_target_hours"}
            if row.kind == "deadline"
            else {"target_date", "estimated_total_hours"}
        ) & sent.keys()
        if wrong:
            raise InvalidMutation(
                "validation_failed",
                f"{', '.join(sorted(wrong))} not valid for a {row.kind} project",
            )
        values = {
            k: (_decimal(v) if k in ("estimated_total_hours", "weekly_target_hours") else v)
            for k, v in sent.items()
        }
        min_m = values.get("min_block_minutes", row.min_block_minutes)
        max_m = values.get("max_block_minutes", row.max_block_minutes)
        if min_m > max_m:
            raise InvalidMutation(
                "validation_failed", "min_block_minutes must be <= max_block_minutes"
            )
        await tx.execute(
            update(Project).where(Project.id == pid, Project.user_id == user_id).values(**values)
        )
        if "requirements" in sent:
            await _add_tags(tx, user_id, sent["requirements"])

    elif isinstance(mutation, ProjectArchived):
        pid = mutation.payload.project_id
        row = await _get_project(tx, user_id, pid)
        if row is None or row.status == "staged":
            raise InvalidMutation("not_found", f"project {pid} not found")
        if row.status == "archived":
            raise InvalidMutation("project_archived", f"project {pid} is archived")
        before = _project_snapshot(row)
        await tx.execute(
            update(Project)
            .where(Project.id == pid, Project.user_id == user_id)
            .values(status="archived")
        )

    elif isinstance(mutation, PreferencesUpdated):
        prefs = mutation.payload.preferences
        old = (
            await tx.execute(
                select(UserPreferences)
                .where(UserPreferences.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if old is not None:
            before = {
                "timezone": old.timezone,
                "schedulable_windows": old.schedulable_windows,
                "day_diversity": old.day_diversity,
                "buffer_minutes": old.buffer_minutes,
                "week_start": old.week_start,
            }
        values = prefs.model_dump(mode="json")
        await tx.execute(
            pg_insert(UserPreferences)
            .values(user_id=user_id, **values)
            .on_conflict_do_update(index_elements=[UserPreferences.user_id], set_=values)
        )

    elif isinstance(mutation, ExceptionAdded):
        e = mutation.payload
        await tx.execute(
            insert(AvailabilityException).values(
                id=e.exception_id,
                user_id=user_id,
                span=to_range(Interval(start=e.span.start, end=e.span.end)),
                blocks_all=e.blocks_all,
                blocks_tags=list(e.blocks_tags),
                reason=e.reason,
                created_at=now,
            )
        )
        await _add_tags(tx, user_id, e.blocks_tags)

    elif isinstance(mutation, ExceptionRemoved):
        eid = mutation.payload.exception_id
        row = (
            await tx.execute(
                delete(AvailabilityException)
                .where(AvailabilityException.id == eid, AvailabilityException.user_id == user_id)
                .returning(
                    AvailabilityException.span,
                    AvailabilityException.blocks_all,
                    AvailabilityException.blocks_tags,
                    AvailabilityException.reason,
                )
            )
        ).one_or_none()
        if row is None:
            raise InvalidMutation("not_found", f"exception {eid} not found")
        before = {
            "span": _span_json(to_interval(row.span)),
            "blocks_all": row.blocks_all,
            "blocks_tags": list(row.blocks_tags),
            "reason": row.reason,
        }

    elif isinstance(mutation, PinAdded):
        p = mutation.payload
        await tx.execute(
            insert(BlockPin).values(
                id=p.pin_id,
                user_id=user_id,
                project_id=p.project_id,
                span=to_range(Interval(start=p.span.start, end=p.span.end)),
                kind=p.kind,
                created_from_block_id=p.created_from_block_id,
                created_at=now,
            )
        )

    elif isinstance(mutation, PinRemoved):
        pin_id = mutation.payload.pin_id
        row = (
            await tx.execute(
                delete(BlockPin)
                .where(BlockPin.id == pin_id, BlockPin.user_id == user_id)
                .returning(
                    BlockPin.project_id,
                    BlockPin.span,
                    BlockPin.kind,
                    BlockPin.created_from_block_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise InvalidMutation("not_found", f"pin {pin_id} not found")
        before = {
            "project_id": None if row.project_id is None else str(row.project_id),
            "span": _span_json(to_interval(row.span)),
            "kind": row.kind,
            "created_from_block_id": (
                None if row.created_from_block_id is None else str(row.created_from_block_id)
            ),
        }

    else:  # pragma: no cover - the union is closed
        raise InvalidMutation("validation_failed", f"unsupported mutation {mutation!r}")

    return await _trace(
        tx,
        user_id,
        kind=mutation.kind,
        payload=mutation.model_dump(mode="json")["payload"],
        before=before,
        interpretation=mutation.interpretation,
        source=mutation.source,
        now=now,
        changeset_id=changeset_id,
        plan_id=plan_id,
    )


async def _trace(
    tx: AsyncSession,
    user_id: uuid.UUID,
    *,
    kind: str,
    payload: Mapping[str, Any],
    now: datetime,
    source: str = "system",
    before: Mapping[str, Any] | None = None,
    interpretation: str | None = None,
    changeset_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
) -> uuid.UUID:
    mutation_id = uuid.uuid4()
    await tx.execute(
        insert(Mutation).values(
            id=mutation_id,
            user_id=user_id,
            changeset_id=changeset_id,
            plan_id=plan_id,
            kind=kind,
            payload=dict(payload),
            before=None if before is None else dict(before),
            interpretation=interpretation,
            source=source,
            created_at=now,
        )
    )
    return mutation_id


# ---------------------------------------------------------------- staging


async def _delete_staged_projects(
    tx: AsyncSession, user_id: uuid.UUID, project_ids: Sequence[uuid.UUID]
) -> None:
    """O1: a staged project dies with its changeset. Its only dependents are blocks of
    proposals that were never promoted (now rejected): those rows go first (the blocks FK
    has no cascade). Pins referencing it cannot exist — pins are inserted only at confirm.
    TODO(ARCH §3): this trims history of rejected plans; the DDL gives no alternative short
    of a deferrable FK."""
    if not project_ids:
        return
    ids = sorted(project_ids)
    live_refs = (
        await tx.execute(
            select(func.count())
            .select_from(Block)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(
                Block.user_id == user_id,
                Block.project_id.in_(ids),
                WeekPlan.status.in_(("live", "superseded", "proposed")),
            )
        )
    ).scalar_one()
    if live_refs:
        raise InvalidMutation(
            "validation_failed", "staged project is referenced by a non-rejected plan"
        )
    await tx.execute(delete(Block).where(Block.user_id == user_id, Block.project_id.in_(ids)))
    await tx.execute(
        delete(Project).where(
            Project.user_id == user_id, Project.id.in_(ids), Project.status == "staged"
        )
    )


async def _close_changeset(
    tx: AsyncSession,
    user_id: uuid.UUID,
    cs: Changeset,
    status: Literal["rejected", "expired"],
    *,
    keep_projects: Sequence[uuid.UUID],
    now: datetime,
) -> None:
    """Reject/expire a pending changeset: plan → rejected, staged projects deleted unless
    carried forward (O9), constraint state untouched."""
    await tx.execute(
        update(Changeset).where(Changeset.id == cs.id).values(status=status, resolved_at=now)
    )
    if cs.plan_id is not None:
        await tx.execute(
            update(WeekPlan)
            .where(WeekPlan.id == cs.plan_id, WeekPlan.status == "proposed")
            .values(status="rejected")
        )
    created = created_project_ids(parse_staged(cs.staged))
    await _delete_staged_projects(tx, user_id, [p for p in created if p not in keep_projects])


async def _pending(tx: AsyncSession, user_id: uuid.UUID) -> list[Changeset]:
    return list(
        (
            await tx.execute(
                select(Changeset)
                .where(Changeset.user_id == user_id, Changeset.status == "pending")
                .order_by(Changeset.created_at)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def stage_changeset(
    tx: AsyncSession,
    user_id: uuid.UUID,
    staged_mutations: Sequence[StagedMutation | Mapping[str, Any]],
    solver_input: SolverInput | None = None,
    solver_output: SolverOutput | None = None,
    *,
    now: datetime,
    timezone: str | None = None,
    source: Literal["agent", "manual", "system"] = "agent",
) -> uuid.UUID:
    """Insert a pending changeset (+ its proposed plan when a solve was possible).

    D4: any previous pending changeset is expired (its plan → `rejected`). Its staged
    projects are deleted (O1) UNLESS the new list carries the same `project_created`
    forward (O9): then the staged row is re-pointed — kept and refreshed from the new
    payload. New `project_created` entries insert their row as `staged` so the proposal's
    blocks satisfy their FK. Constraint state is not touched.

    `solver_input`/`solver_output` must both be given or both be None (onboarding before
    preferences: plan_id null). `timezone` defaults to the overlaid preferences timezone.
    TODO(contracts): `staged` has minItems 1 in the contract; an empty list (a drift
    re-solve with no constraint change, T4.3) is accepted here pending a decision.
    """
    if (solver_input is None) != (solver_output is None):
        raise ValueError("solver_input and solver_output go together")
    await lock_user(tx, user_id)
    staged = parse_staged(staged_mutations)
    new_created = created_project_ids(staged)

    # Fail at staging, not at confirm, if the list cannot apply to committed state.
    prefs = overlay(await load_constraint_state(tx, user_id), staged).preferences
    if solver_input is None and prefs is not None:
        # A planless changeset is only for onboarding, before any preferences exist: once a
        # solve is possible, confirming without one would leave the live plan unreconciled.
        raise InvalidMutation(
            "validation_failed", "preferences exist: a changeset must carry its proposed plan"
        )
    if solver_input is not None:
        if prefs is None:
            raise PreferencesNotSet("no preferences: cannot store a plan")
        _check_horizon(solver_input.horizon, now, timezone or prefs.timezone, prefs.week_start)

    for old in await _pending(tx, user_id):
        await _close_changeset(tx, user_id, old, "expired", keep_projects=new_created, now=now)
    if solver_input is not None:
        await _check_supersedable(tx, user_id, solver_input.horizon, now)

    for m in staged:
        if not isinstance(m, ProjectCreated):
            continue
        row = await _get_project(tx, user_id, m.payload.project_id)
        values = _project_values(m)
        if row is None:
            await tx.execute(
                insert(Project).values(
                    id=m.payload.project_id,
                    user_id=user_id,
                    status="staged",
                    created_at=now,
                    **values,
                )
            )
        elif row.status == "staged":  # O9 carry-forward: re-point, refresh from the new draft
            await tx.execute(
                update(Project)
                .where(Project.id == row.id, Project.user_id == user_id)
                .values(**values)
            )
        else:
            raise InvalidMutation("project_exists", f"project {row.id} already exists")

    plan_id: uuid.UUID | None = None
    if solver_input is not None and solver_output is not None and prefs is not None:
        plan_id = await persist_plan(
            tx,
            user_id,
            solver_input,
            solver_output,
            now=now,
            timezone=timezone or prefs.timezone,
            week_start=prefs.week_start,
        )

    changeset_id = uuid.uuid4()
    await tx.execute(
        insert(Changeset).values(
            id=changeset_id,
            user_id=user_id,
            plan_id=plan_id,
            staged=dump_staged(staged),
            status="pending",
            created_at=now,
        )
    )
    if plan_id is not None:
        await _trace(
            tx,
            user_id,
            kind="plan_proposed",
            payload={"plan_id": str(plan_id), "changeset_id": str(changeset_id)},
            source=source,
            now=now,
            changeset_id=changeset_id,
            plan_id=plan_id,
        )
    return changeset_id


async def carry_forward(
    tx: AsyncSession,
    user_id: uuid.UUID,
    new_mutations: Sequence[StagedMutation | Mapping[str, Any]],
) -> list[StagedMutation]:
    """O9: the staged list for a follow-up write. If any new mutation targets a project that
    the pending changeset is creating (update / archive / pin on it), the pending list is
    carried forward in front of the new mutations, so creation + follow-up confirm together
    and `stage_changeset` re-points the staged row instead of deleting it. Otherwise the new
    mutations stand alone (D4: the older pending changeset simply expires)."""
    await lock_user(tx, user_id)
    new = parse_staged(new_mutations)
    pending = await _pending(tx, user_id)
    if not pending:
        return new
    old = parse_staged(pending[-1].staged)
    creating = set(created_project_ids(old))
    targets: set[uuid.UUID] = set()
    for m in new:
        if isinstance(m, ProjectUpdated | ProjectArchived):
            targets.add(m.payload.project_id)
        elif isinstance(m, PinAdded) and m.payload.project_id is not None:
            targets.add(m.payload.project_id)
    return [*old, *new] if targets & creating else new


async def propose_changeset(
    tx: AsyncSession,
    user_id: uuid.UUID,
    staged_mutations: Sequence[StagedMutation | Mapping[str, Any]],
    *,
    now: datetime,
    horizon: Interval | None = None,
    busy: Sequence[Interval] = (),
    source: Literal["agent", "manual", "system"] = "agent",
) -> uuid.UUID:
    """Convenience for T2.3/T3: solve the hypothetical state (committed + staged) over the
    current preference week (`expected_horizon` of the overlaid preferences, unless an
    explicit `horizon` is given — which must equal it) with the live plan's remainder as
    `previous`, then `stage_changeset`. When no preferences exist yet (onboarding) the
    changeset is stored without a plan."""
    await lock_user(tx, user_id)
    staged = parse_staged(staged_mutations)
    state = overlay(await load_constraint_state(tx, user_id), staged)
    if state.preferences is None:
        return await stage_changeset(tx, user_id, staged, now=now, source=source)
    tz = state.preferences.timezone
    if horizon is None:
        horizon = expected_horizon(now, tz, state.preferences.week_start)
    solver_input = await build_solver_input(tx, user_id, horizon, now, tz, staged=staged, busy=busy)
    previous = await load_previous(tx, user_id, horizon, now)
    solver_output = pack(solver_input, previous)
    return await stage_changeset(
        tx, user_id, staged, solver_input, solver_output, now=now, timezone=tz, source=source
    )


# ---------------------------------------------------------------- confirm / reject


@dataclass(frozen=True)
class ConfirmResult:
    changeset_id: uuid.UUID
    plan_id: uuid.UUID | None  # the now-live plan; None for a plan-less (onboarding) changeset
    already_confirmed: bool  # idempotent replay: nothing was written


async def _get_changeset(
    tx: AsyncSession, user_id: uuid.UUID, changeset_id: uuid.UUID
) -> Changeset:
    cs = (
        await tx.execute(
            select(Changeset)
            .where(Changeset.id == changeset_id, Changeset.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if cs is None:
        raise ChangesetNotFound(str(changeset_id))
    return cs


def _schedule_key(blocks: Sequence[tuple[str, Interval]]) -> list[tuple[str, Any, Any]]:
    return sorted((pid, iv.start, iv.end) for pid, iv in blocks)


async def _stale_check(
    tx: AsyncSession,
    user_id: uuid.UUID,
    plan: WeekPlan,
    staged: Sequence[StagedMutation],
    *,
    now: datetime,
    busy: Sequence[Interval] | None,
) -> SolverInput:
    """O8. Re-solve current state + the staged mutations at `now` (stored proposal blocks as
    `previous`, for maximal stability). Stale if the frozen set (lineage, project, span)
    changed — promoting would drop or move a started block — or if the new blocks differ in
    (project, span). Lineage ids and infeasibility text are not compared: the user reviewed
    the schedule, and the stored proposal (with its lineages) is what gets promoted."""
    stored_input = SolverInput.model_validate(plan.solver_input)
    frozen_lineages = {f.lineage_id for f in stored_input.frozen}
    rows = (
        await tx.execute(
            select(Block).where(Block.plan_id == plan.id).execution_options(populate_existing=True)
        )
    ).scalars()
    stored_new = [
        PlannedBlock(
            lineage_id=str(b.lineage_id), project_id=str(b.project_id), span=to_interval(b.span)
        )
        for b in rows
        if str(b.lineage_id) not in frozen_lineages
    ]
    fresh_input = await build_solver_input(
        tx,
        user_id,
        to_interval(plan.horizon),
        now,
        plan.timezone,
        staged=staged,
        busy=stored_input.busy if busy is None else busy,
    )
    fresh_output = pack(fresh_input, stored_new)

    def frozen_key(inp: SolverInput) -> list[tuple[str, str, Any, Any]]:
        return sorted((f.lineage_id, f.project_id, f.span.start, f.span.end) for f in inp.frozen)

    if frozen_key(fresh_input) != frozen_key(stored_input):
        raise ChangesetStale(fresh_input, fresh_output, "started blocks changed since proposal")
    if _schedule_key([(b.project_id, b.span) for b in fresh_output.blocks]) != _schedule_key(
        [(b.project_id, b.span) for b in stored_new]
    ):
        raise ChangesetStale(fresh_input, fresh_output, "re-solve differs from the proposal")
    return stored_input


async def _inherit_from_live(
    tx: AsyncSession,
    new_plan_id: uuid.UUID,
    old_plan_ids: Sequence[uuid.UUID],
    frozen_lineages: set[str],
) -> None:
    """Carry each lineage's outbox identity into the promoted plan.

    Frozen copies take the old row verbatim (state included: complete/skip/sweep since the
    proposal must not be lost). Other reused lineages keep their gcal event id/etag; their
    `gcal_status` stays as it was only if project and span are unchanged, else `pending`
    (the outbox must PATCH the event). New lineages are already `pending`."""
    if not old_plan_ids:
        return
    old = {
        str(b.lineage_id): b
        for b in (
            await tx.execute(
                select(Block)
                .where(Block.plan_id.in_(sorted(old_plan_ids)))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    new_rows = (
        await tx.execute(
            select(Block)
            .where(Block.plan_id == new_plan_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    for b in list(new_rows):
        src = old.get(str(b.lineage_id))
        if src is None:
            continue
        values: dict[str, Any] = {"gcal_event_id": src.gcal_event_id, "gcal_etag": src.gcal_etag}
        if str(b.lineage_id) in frozen_lineages:
            values |= {"state": src.state, "gcal_status": src.gcal_status}
        elif src.project_id == b.project_id and src.span == b.span:
            values["gcal_status"] = src.gcal_status
        else:
            values["gcal_status"] = "pending"
        await tx.execute(update(Block).where(Block.id == b.id).values(**values))


async def confirm_changeset(
    tx: AsyncSession,
    user_id: uuid.UUID,
    changeset_id: uuid.UUID,
    *,
    now: datetime,
    busy: Sequence[Interval] | None = None,
) -> ConfirmResult:
    """Confirm under the per-user advisory lock (ARCH §3 Semantics):

    1. not pending → ChangesetNotPending(status); already confirmed → idempotent no-op;
    2. O8 stale check (see `_stale_check`) → ChangesetStale, nothing applied; a planless
       changeset reaching a user who now has preferences → InvalidMutation; a live plan
       whose upcoming blocks fall outside the horizon → InvalidMutation (`_check_supersedable`);
    3. apply staged mutations via `apply_mutation` (staged projects → `active`);
    4. lazy-sweep past blocks (so the superseded plan's finished work is `completed` and
       stays credited), then supersede the old live plan(s) over the horizon, STRICTLY BEFORE
    5. promoting the proposal to `live`;
    6. inherit lineage outbox fields / refresh frozen copies; changed blocks → `pending`;
    7. changeset → confirmed + `plan_confirmed` trace row.

    `busy`: fresh freeBusy for the stale check (T4.2); None reuses the proposal's busy.
    """
    await lock_user(tx, user_id)
    cs = await _get_changeset(tx, user_id, changeset_id)
    if cs.status == "confirmed":
        return ConfirmResult(changeset_id, cs.plan_id, already_confirmed=True)
    if cs.status != "pending":
        raise ChangesetNotPending(cs.status)
    staged = parse_staged(cs.staged)

    plan: WeekPlan | None = None
    stored_input: SolverInput | None = None
    to_supersede: list[uuid.UUID] = []
    if cs.plan_id is not None:
        plan = (
            await tx.execute(
                select(WeekPlan)
                .where(WeekPlan.id == cs.plan_id, WeekPlan.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        stored_input = await _stale_check(tx, user_id, plan, staged, now=now, busy=busy)
        to_supersede = await _check_supersedable(tx, user_id, to_interval(plan.horizon), now)
    elif await _preference_week(tx, user_id, staged) is not None:
        raise InvalidMutation(
            "validation_failed",
            "preferences now exist: re-propose so the change is solved against the live plan",
        )

    for m in staged:
        await apply_mutation(tx, user_id, m, now=now, changeset_id=cs.id, plan_id=cs.plan_id)

    if plan is not None and stored_input is not None:
        await sweep_past_blocks(tx, user_id, now)
        superseded = list(
            (
                await tx.execute(
                    update(WeekPlan)
                    .where(WeekPlan.id.in_(to_supersede), WeekPlan.status == "live")
                    .values(status="superseded", superseded_at=now)
                    .returning(WeekPlan.id)
                )
            ).scalars()
        )
        await tx.execute(update(WeekPlan).where(WeekPlan.id == plan.id).values(status="live"))
        await _inherit_from_live(
            tx, plan.id, superseded, {f.lineage_id for f in stored_input.frozen}
        )

    await tx.execute(
        update(Changeset).where(Changeset.id == cs.id).values(status="confirmed", resolved_at=now)
    )
    await _trace(
        tx,
        user_id,
        kind="plan_confirmed",
        payload={"changeset_id": str(cs.id), "plan_id": None if plan is None else str(plan.id)},
        source=staged[0].source if staged else "system",
        now=now,
        changeset_id=cs.id,
        plan_id=cs.plan_id,
    )
    return ConfirmResult(changeset_id, cs.plan_id, already_confirmed=False)


async def reject_changeset(
    tx: AsyncSession, user_id: uuid.UUID, changeset_id: uuid.UUID, *, now: datetime
) -> None:
    """Plan → `rejected`, staged projects deleted (O1), constraint state untouched.
    Idempotent on an already-rejected changeset; other non-pending → ChangesetNotPending."""
    await lock_user(tx, user_id)
    cs = await _get_changeset(tx, user_id, changeset_id)
    if cs.status == "rejected":
        return
    if cs.status != "pending":
        raise ChangesetNotPending(cs.status)
    await _close_changeset(tx, user_id, cs, "rejected", keep_projects=(), now=now)
    await _trace(
        tx,
        user_id,
        kind="plan_rejected",
        payload={
            "changeset_id": str(cs.id),
            "plan_id": None if cs.plan_id is None else str(cs.plan_id),
        },
        now=now,
        changeset_id=cs.id,
        plan_id=cs.plan_id,
    )


# ---------------------------------------------------------------- block state


async def sweep_past_blocks(tx: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """Lazy sweep (ARCH §3): live-plan blocks that ended by `now` and are still `planned`
    become `completed` (the default; "missed" would poison remaining-effort math).
    Returns the number of rows swept. Call before reads that show or solve the week."""
    await lock_user(tx, user_id)
    live_plans = select(WeekPlan.id).where(WeekPlan.user_id == user_id, WeekPlan.status == "live")
    result = await tx.execute(
        update(Block)
        .where(
            Block.user_id == user_id,
            Block.state == "planned",
            func.upper(Block.span) <= now,
            Block.plan_id.in_(live_plans),
        )
        .values(state="completed")
        .returning(Block.id)
    )
    return len(result.all())


async def mark_block_state(
    tx: AsyncSession,
    user_id: uuid.UUID,
    block_id: uuid.UUID,
    state: Literal["completed", "skipped"],
    *,
    now: datetime,
) -> None:
    """`PATCH /blocks/{id}` complete|skip (contracts D5): applies directly, no re-solve, only
    on a started block of a live plan. A pending proposal is not expired; its confirm
    reconciles via the O8 check and the frozen-copy refresh."""
    await lock_user(tx, user_id)
    row = (
        await tx.execute(
            select(Block, WeekPlan.status)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(Block.id == block_id, Block.user_id == user_id)
            .with_for_update(of=Block)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise BlockNotFound(str(block_id))
    block, plan_status = row
    if plan_status != "live":
        raise BlockSuperseded(str(block_id))
    if block.span.lower is None or block.span.lower > now:
        raise BlockNotStarted(str(block_id))
    await tx.execute(update(Block).where(Block.id == block_id).values(state=state))

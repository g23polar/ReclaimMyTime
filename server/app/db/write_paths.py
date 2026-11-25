"""The only write entry points for scheduling state and plans (ARCHITECTURE.md §3 Semantics;
CLAUDE.md rules 1, 2, 3, 5).

- `persist_plan` is the sole creator of `week_plans` + `blocks`, and refuses any plan that
  `scheduler.verify()` rejects.
- `apply_mutation` is the sole mutator of projects / preferences / exceptions / pins /
  user_tags, and writes the `mutations` trace row in the same transaction.
- `stage_changeset` / `confirm_changeset` / `reject_changeset` implement propose → confirm.
- `sweep_past_blocks` / `mark_block_state` are the only block-state writes.
- `apply_google_edits` is the only path that applies an edit made in Google Calendar
  directly (T4.3b, "hybrid"); `propose_drift` stages the drift re-solve.

Transaction discipline (same as app/auth/repository.py): the caller owns `tx` and commits;
nothing here commits or rolls back. A raised exception means the caller must roll back.
Every function takes the per-user advisory lock (re-entrant within a transaction), with the
exact key auth uses. Nothing reads the clock: `now` is injected and also stamps every
`created_at` / `resolved_at` / `superseded_at` written here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    MAX_USER_TAGS,
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
    check_row_caps,
    created_project_ids,
    dump_staged,
    parse_staged,
    staged_tags,
)
from app.db.solver_io import (
    PreferencesNotSet,
    SourceBlock,
    block_rows,
    build_solver_input,
    clip_intervals,
    load_constraint_state,
    load_previous,
    overlay,
    to_interval,
    to_range,
    week_horizon,
    week_start_for,
)
from app.scheduler import (
    Infeasibility,
    Interval,
    PlannedBlock,
    SolverInput,
    SolverOutput,
    Violation,
    pack,
    verify,
)

__all__ = [
    "BlockNotFound",
    "BlockNotStarted",
    "BlockSuperseded",
    "ChangesetNotFound",
    "ChangesetNotPending",
    "ChangesetStale",
    "ConfirmResult",
    "GoogleEdit",
    "GoogleEditOutcome",
    "InvalidMutation",
    "PlanVerificationError",
    "PreferencesNotSet",
    "Reason",
    "WritePathError",
    "apply_google_edits",
    "apply_mutation",
    "carry_forward",
    "confirm_changeset",
    "expected_horizon",
    "lock_user",
    "mark_block_state",
    "persist_plan",
    "preview_carry_forward",
    "propose_changeset",
    "propose_drift",
    "reject_changeset",
    "solve_horizon",
    "stage_changeset",
    "sweep_past_blocks",
    "user_lock_statement",
]


# Why a changeset was proposed (`changesets.reason`, migration 0002; T4.3 contract). REST
# staging is `user_edit`, agent turns `agent`; `drift` / `google_edit` come from T4.3 part (b).
Reason = Literal["user_edit", "agent", "drift", "google_edit"]
_REASON_BY_SOURCE: dict[str, Reason] = {"agent": "agent", "manual": "user_edit"}
# Reasons whose changeset may stage no mutation (D25; google_edit added 2026-11-16, T4.3b).
_MAY_STAGE_NOTHING: frozenset[str] = frozenset({"drift", "google_edit"})


def _reason_for(source: str, reason: Reason | None) -> Reason:
    """An explicit `reason` wins. Otherwise it follows the staging source (the agent route and
    REST staging pass none); a `system` source has no implied reason and must name one."""
    if reason is not None:
        return reason
    try:
        return _REASON_BY_SOURCE[source]
    except KeyError:
        raise ValueError(f"source {source!r} requires an explicit changeset reason") from None


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


async def _vocabulary(tx: AsyncSession, user_id: uuid.UUID) -> set[str]:
    return set((await tx.execute(select(UserTag.tag).where(UserTag.user_id == user_id))).scalars())


def _check_tag_cap(vocabulary: set[str], tags: Sequence[str]) -> None:
    """Issue #7: refuse a write that adds a tag past `MAX_USER_TAGS`. A write that adds no
    new tag always passes, so a user already over the cap keeps using their tags."""
    new = set(tags) - vocabulary
    if new and len(vocabulary) + len(new) > MAX_USER_TAGS:
        raise InvalidMutation(
            "validation_failed",
            f"new tags {sorted(new)} would exceed the tag limit. "
            f"You can have at most {MAX_USER_TAGS} tags.",
        )


async def _check_row_caps(
    tx: AsyncSession, user_id: uuid.UUID, staged: Sequence[StagedMutation], now: datetime
) -> None:
    """Issue #24: `check_row_caps` against the committed rows in use at `now`."""

    async def ids(stmt: Any) -> list[uuid.UUID]:
        return list((await tx.execute(stmt)).scalars())

    check_row_caps(
        projects=await ids(
            select(Project.id).where(
                Project.user_id == user_id, Project.status.in_(("active", "paused", "done"))
            )
        ),
        exceptions=await ids(
            select(AvailabilityException.id).where(
                AvailabilityException.user_id == user_id,
                func.upper(AvailabilityException.span) > now,
            )
        ),
        pins=await ids(
            select(BlockPin.id).where(BlockPin.user_id == user_id, func.upper(BlockPin.span) > now)
        ),
        staged=staged,
        now=now,
    )


async def _add_tags(tx: AsyncSession, user_id: uuid.UUID, tags: Sequence[str]) -> None:
    # contracts O3 (assumed): a tag first used by a confirmed write joins the vocabulary.
    # Counted in this transaction, so earlier mutations of the same confirm count too.
    if tags:
        _check_tag_cap(await _vocabulary(tx, user_id), tags)
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


async def _repend_project_events(
    tx: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID, now: datetime
) -> None:
    """A rename changes the event title (= project name, T4.3a): the project's live blocks
    that have not ended go back to `pending` so the write-back outbox updates their events.
    Ended blocks keep the old title (Principle 4: past events are never rewritten). Inside a
    confirm this runs before the live plan is superseded, so the promoted plan inherits the
    `pending` (`_inherit_from_live`). The caller kicks the drain after commit."""
    live = select(WeekPlan.id).where(WeekPlan.user_id == user_id, WeekPlan.status == "live")
    await tx.execute(
        update(Block)
        .where(
            Block.user_id == user_id,
            Block.project_id == project_id,
            Block.gcal_status == "synced",
            func.upper(Block.span) > now,
            Block.plan_id.in_(live.scalar_subquery()),
        )
        .values(gcal_status="pending")
    )


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
        renamed = "name" in sent and sent["name"] != row.name
        await tx.execute(
            update(Project).where(Project.id == pid, Project.user_id == user_id).values(**values)
        )
        if "requirements" in sent:
            await _add_tags(tx, user_id, sent["requirements"])
        if renamed:
            await _repend_project_events(tx, user_id, pid, now)

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
    reason: Reason | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a pending changeset (+ its proposed plan when a solve was possible).

    D4: any previous pending changeset is expired (its plan → `rejected`). Its staged
    projects are deleted (O1) UNLESS the new list carries the same `project_created`
    forward (O9): then the staged row is re-pointed — kept and refreshed from the new
    payload. New `project_created` entries insert their row as `staged` so the proposal's
    blocks satisfy their FK. Constraint state is not touched.

    `solver_input`/`solver_output` must both be given or both be None (onboarding before
    preferences: plan_id null). `timezone` defaults to the overlaid preferences timezone.
    `reason` (contract `Changeset.reason`) defaults from `source` (`_reason_for`). An empty
    `staged` list is allowed only for a `drift` re-solve (contract D25, resolving O11) and for
    a `google_edit` knock-on re-solve whose pin was already applied directly (2026-11-16,
    T4.3b). `created_at` (default `now`) stamps the changeset row only; a restage passes a
    write-time stamp later than the changeset it replaces (T4.3b review round 2).
    """
    if (solver_input is None) != (solver_output is None):
        raise ValueError("solver_input and solver_output go together")
    reason = _reason_for(source, reason)
    if not staged_mutations and reason not in _MAY_STAGE_NOTHING:
        raise InvalidMutation(
            "validation_failed", "only a drift or google_edit changeset may stage no mutations"
        )
    await lock_user(tx, user_id)
    if solver_input is not None:
        # M1+M2 gate: ended work is `completed` before anything is stored against it.
        await sweep_past_blocks(tx, user_id, now)
    staged = parse_staged(staged_mutations)
    new_created = created_project_ids(staged)

    # Fail at staging, not at confirm, if the list cannot apply to committed state.
    _check_tag_cap(await _vocabulary(tx, user_id), staged_tags(staged))
    await _check_row_caps(tx, user_id, staged, now)
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
            reason=reason,
            created_at=now if created_at is None else created_at,
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


def _carried(
    pending_staged: Sequence[StagedMutation] | None, new: list[StagedMutation]
) -> list[StagedMutation]:
    if not pending_staged:
        return new
    old = list(pending_staged)
    creating = set(created_project_ids(old))
    targets: set[uuid.UUID] = set()
    for m in new:
        if isinstance(m, ProjectUpdated | ProjectArchived):
            targets.add(m.payload.project_id)
        elif isinstance(m, PinAdded) and m.payload.project_id is not None:
            targets.add(m.payload.project_id)
    return [*old, *new] if targets & creating else new


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
    return _carried(parse_staged(pending[-1].staged) if pending else None, new)


async def preview_carry_forward(
    tx: AsyncSession,
    user_id: uuid.UUID,
    new_mutations: Sequence[StagedMutation | Mapping[str, Any]],
) -> list[StagedMutation]:
    """`carry_forward` WITHOUT the lock or row locks: a best-effort preview used to decide the
    busy-time window before locking (T4.2: no Google call under the per-user lock). Callers
    re-derive the list under the lock and compare horizons (`solve_horizon`)."""
    new = parse_staged(new_mutations)
    latest = (
        await tx.execute(
            select(Changeset.staged)
            .where(Changeset.user_id == user_id, Changeset.status == "pending")
            .order_by(Changeset.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _carried(parse_staged(latest) if latest is not None else None, new)


async def solve_horizon(
    tx: AsyncSession, user_id: uuid.UUID, staged: Sequence[StagedMutation], now: datetime
) -> Interval | None:
    """The horizon `propose_changeset` would solve `staged` over at `now` (the overlaid
    preference week), or None when no preferences exist yet (no solve, so no busy time)."""
    week = await _preference_week(tx, user_id, staged)
    return None if week is None else expected_horizon(now, *week)


async def propose_changeset(
    tx: AsyncSession,
    user_id: uuid.UUID,
    staged_mutations: Sequence[StagedMutation | Mapping[str, Any]],
    *,
    now: datetime,
    horizon: Interval | None = None,
    busy: Sequence[Interval] = (),
    source: Literal["agent", "manual", "system"] = "agent",
    reason: Reason | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Convenience for T2.3/T3: solve the hypothetical state (committed + staged) over the
    current preference week (`expected_horizon` of the overlaid preferences, unless an
    explicit `horizon` is given — which must equal it) with the live plan's remainder as
    `previous`, then `stage_changeset`. When no preferences exist yet (onboarding) the
    changeset is stored without a plan."""
    await lock_user(tx, user_id)
    # M1+M2 gate: credit ended-but-unswept blocks (completed_minutes counts only `completed`
    # rows) before solving — they may lie outside the new horizon (week-start change), where
    # no frozen copy would otherwise account for them.
    await sweep_past_blocks(tx, user_id, now)
    staged = parse_staged(staged_mutations)
    state = overlay(await load_constraint_state(tx, user_id), staged)
    if state.preferences is None:
        return await stage_changeset(
            tx, user_id, staged, now=now, source=source, reason=reason, created_at=created_at
        )
    tz = state.preferences.timezone
    if horizon is None:
        horizon = expected_horizon(now, tz, state.preferences.week_start)
    solver_input = await build_solver_input(tx, user_id, horizon, now, tz, staged=staged, busy=busy)
    previous = await load_previous(tx, user_id, horizon, now)
    solver_output = pack(solver_input, previous)
    return await stage_changeset(
        tx,
        user_id,
        staged,
        solver_input,
        solver_output,
        now=now,
        timezone=tz,
        source=source,
        reason=reason,
        created_at=created_at,
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
    the schedule, and the stored proposal (with its lineages) is what gets promoted.

    `busy` (T4.2) is FRESH freeBusy, or None (no fresh read: the proposal's busy is reused).
    With fresh busy: a future proposed block that overlaps it is stale outright (decision
    T4.2 #4); otherwise the comparison re-solves against the proposal's own busy (busy that
    merely changed elsewhere does not invalidate what the user reviewed). Every replacement
    proposal carried by ChangesetStale is solved against the fresh busy."""
    stored_input = SolverInput.model_validate(plan.solver_input)
    frozen_lineages = {f.lineage_id for f in stored_input.frozen}
    horizon = to_interval(plan.horizon)
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

    async def solve(with_busy: Sequence[Interval]) -> tuple[SolverInput, SolverOutput]:
        inp = await build_solver_input(
            tx, user_id, horizon, now, plan.timezone, staged=staged, busy=with_busy
        )
        return inp, pack(inp, stored_new)

    fresh_busy = None if busy is None else clip_intervals(busy, horizon)
    if fresh_busy is not None and any(
        b.span.start >= now and any(iv.overlaps(b.span) for iv in fresh_busy) for b in stored_new
    ):
        raise ChangesetStale(*await solve(fresh_busy), "calendar busy time overlaps the proposal")

    fresh_input, fresh_output = await solve(stored_input.busy)

    def frozen_key(inp: SolverInput) -> list[tuple[str, str, Any, Any]]:
        return sorted((f.lineage_id, f.project_id, f.span.start, f.span.end) for f in inp.frozen)

    reason: str | None = None
    if frozen_key(fresh_input) != frozen_key(stored_input):
        reason = "started blocks changed since proposal"
    elif _schedule_key([(b.project_id, b.span) for b in fresh_output.blocks]) != _schedule_key(
        [(b.project_id, b.span) for b in stored_new]
    ):
        reason = "re-solve differs from the proposal"
    if reason is not None:
        if fresh_busy is not None and fresh_busy != stored_input.busy:
            fresh_input, fresh_output = await solve(fresh_busy)
        raise ChangesetStale(fresh_input, fresh_output, reason)
    return stored_input


async def _check_current_week(
    tx: AsyncSession,
    user_id: uuid.UUID,
    plan: WeekPlan,
    staged: Sequence[StagedMutation],
    *,
    now: datetime,
    busy: Sequence[Interval] | None,
) -> None:
    """M1+M2 gate finding 3: a proposal for a week that has ended is stale even when re-solving
    its own horizon compares equal (an empty schedule, or one that is entirely frozen).
    Raises ChangesetStale carrying a solve of the staged mutations over the CURRENT
    preference week (overlaid preferences), for re-review; nothing is applied."""
    week = await _preference_week(tx, user_id, staged)
    if week is None:  # pragma: no cover - a planned changeset implies preferences
        raise PreferencesNotSet("no preferences: cannot confirm a plan")
    tz, week_start = week
    current = expected_horizon(now, tz, week_start)
    if to_interval(plan.horizon) == current:
        return
    fresh_input = await build_solver_input(
        tx, user_id, current, now, tz, staged=staged, busy=() if busy is None else busy
    )
    previous = await load_previous(tx, user_id, current, now)
    raise ChangesetStale(fresh_input, pack(fresh_input, previous), "the proposal's week has ended")


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
    2. lazy-sweep, then: proposal horizon ≠ the current preference week → ChangesetStale with
       a solve over the current week (`_check_current_week`); O8 stale check (see
       `_stale_check`) → ChangesetStale, nothing applied; a planless
       changeset reaching a user who now has preferences → InvalidMutation; a live plan
       whose upcoming blocks fall outside the horizon → InvalidMutation (`_check_supersedable`);
       a list past a per-user row cap → LimitReached (issue #24, `check_row_caps`);
    3. apply staged mutations via `apply_mutation` (staged projects → `active`);
    4. lazy-sweep past blocks (so the superseded plan's finished work is `completed` and
       stays credited), then supersede the old live plan(s) over the horizon, STRICTLY BEFORE
    5. promoting the proposal to `live`;
    6. inherit lineage outbox fields / refresh frozen copies; changed blocks → `pending`;
    7. changeset → confirmed + `plan_confirmed` trace row.

    `busy`: FRESH freeBusy (T4.2), fetched by the caller before this transaction; it may span
    more than one week (it is clipped per solve). It drives the week-rollover re-solve and the
    busy-overlap rule in `_stale_check`. None (no fresh read) reuses the proposal's busy.
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
        await sweep_past_blocks(tx, user_id, now)  # gate finding 2: credit ended work first
        await _check_current_week(tx, user_id, plan, staged, now=now, busy=busy)
        stored_input = await _stale_check(tx, user_id, plan, staged, now=now, busy=busy)
        to_supersede = await _check_supersedable(tx, user_id, to_interval(plan.horizon), now)
    elif await _preference_week(tx, user_id, staged) is not None:
        raise InvalidMutation(
            "validation_failed",
            "preferences now exist: re-propose so the change is solved against the live plan",
        )

    await _check_row_caps(tx, user_id, staged, now)
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
) -> int:
    """Plan → `rejected`, staged projects deleted (O1), constraint state untouched.
    Idempotent on an already-rejected changeset; other non-pending → ChangesetNotPending.

    Rejecting a `google_edit` changeset that carries staged pins (case iii: nothing was
    applied) undoes the edit made in Google (user decision 2026-11-16, T4.3b): the live blocks
    its pins came from go back to `pending`, so the write-back puts our planned time back over
    the user's event and the next sync sees our etag (nothing is re-proposed). An empty
    `google_edit` (the edit already went live, pinned) only declines the knock-on: nothing is
    re-pended (review round 1). Returns how many blocks were re-pended; the caller kicks
    the drain after commit when that is non-zero."""
    await lock_user(tx, user_id)
    cs = await _get_changeset(tx, user_id, changeset_id)
    if cs.status == "rejected":
        return 0
    if cs.status != "pending":
        raise ChangesetNotPending(cs.status)
    await _close_changeset(tx, user_id, cs, "rejected", keep_projects=(), now=now)
    staged = parse_staged(cs.staged)
    # Review round 1 (user decision): only a google_edit that carries staged pins (case iii:
    # nothing was applied) undoes the Google move. An EMPTY one is a knock-on whose edit is
    # already live and pinned: rejecting it only declines the knock-on.
    repended = (
        await _repend_google_edit_sources(tx, user_id, staged, now)
        if cs.reason == "google_edit" and staged
        else 0
    )
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
    return repended


async def _repend_google_edit_sources(
    tx: AsyncSession, user_id: uuid.UUID, staged: Sequence[StagedMutation], now: datetime
) -> int:
    """The live, not-ended rows of the lineage-weeks a rejected `google_edit` changeset's pins
    were created from (`created_from_block_id`) → `pending`. Only sync columns change."""
    sources = sorted(
        {
            m.payload.created_from_block_id
            for m in staged
            if isinstance(m, PinAdded) and m.payload.created_from_block_id is not None
        }
    )
    if not sources:
        return 0
    keys = (
        await tx.execute(
            select(Block.lineage_id, WeekPlan.week_start)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(Block.user_id == user_id, Block.id.in_(sources))
        )
    ).all()
    total = 0
    for lineage_id, week_start in keys:
        live = select(WeekPlan.id).where(
            WeekPlan.user_id == user_id,
            WeekPlan.status == "live",
            WeekPlan.week_start == week_start,
        )
        result = await tx.execute(
            update(Block)
            .where(
                Block.user_id == user_id,
                Block.lineage_id == lineage_id,
                Block.gcal_status == "synced",
                func.upper(Block.span) > now,
                Block.plan_id.in_(live.scalar_subquery()),
            )
            .values(gcal_status="pending")
        )
        total += int(result.rowcount)  # type: ignore[attr-defined]
    return total


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


# ---------------------------------------------------------------- Google edits + drift (T4.3b)


@dataclass(frozen=True)
class GoogleEdit:
    """An edit the user made to one of our events inside Google Calendar, detected BEFORE the
    lock (app.gcal.ingest). `block_id`/`expected_etag`/`old_span` are what the row looked like
    when Google was read; `apply_google_edits` re-validates them under the lock (compare-and-
    set) and skips the edit when anything changed (the next sync re-evaluates it)."""

    kind: Literal["move", "delete"]
    block_id: uuid.UUID
    lineage_id: uuid.UUID
    old_span: Interval
    event_id: str
    expected_etag: str | None
    new_span: Interval | None = None  # move only
    new_etag: str | None = None  # move only: the event's etag, stored once applied

    @property
    def interpretation(self) -> str:
        return "Moved in Google Calendar" if self.kind == "move" else "Deleted in Google Calendar"


@dataclass(frozen=True)
class GoogleEditOutcome:
    applied: int = 0  # edits applied directly (a new live plan each)
    staged: int = 0  # edits whose pins went into the proposal (case iii / fallback)
    skipped: int = 0  # state changed since Google was read, or a pending changeset blocked it
    changeset_id: uuid.UUID | None = None  # the one combined `google_edit` proposal, if any


def _block_key(blocks: Sequence[PlannedBlock]) -> list[tuple[str, str, Any, Any]]:
    return sorted((b.lineage_id, b.project_id, b.span.start, b.span.end) for b in blocks)


def _with_pin_flags(blocks: Sequence[PlannedBlock], inp: SolverInput) -> list[PlannedBlock]:
    """`pinned` = the block exactly realizes a pin-kind pin of `inp` (what verify() expects)."""
    pinned = {(p.project_id, p.span) for p in inp.pins if p.kind == "pin"}
    return [
        b.model_copy(update={"pinned": (b.project_id, b.span) in pinned})
        for b in sorted(blocks, key=lambda b: (b.span.start, b.lineage_id))
    ]


async def _valid_google_edit(
    tx: AsyncSession, user_id: uuid.UUID, e: GoogleEdit, horizon: Interval, now: datetime
) -> bool:
    """Compare-and-set re-validation under the lock: the row is still in the live plan of the
    current week, still `synced` with the etag we compared, same event and span, not started;
    a move's new span still starts in the future."""
    row = (
        await tx.execute(
            select(Block, WeekPlan.status, WeekPlan.horizon)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(Block.id == e.block_id, Block.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return False
    block, plan_status, plan_horizon = row
    return (
        plan_status == "live"
        and to_interval(plan_horizon) == horizon
        and block.gcal_status == "synced"
        and block.gcal_etag == e.expected_etag
        and block.gcal_event_id == e.event_id
        and block.lineage_id == e.lineage_id
        and to_interval(block.span) == e.old_span
        and e.old_span.start > now
        and (e.kind == "delete" or (e.new_span is not None and e.new_span.start > now))
    )


async def _google_edit_mutations(
    tx: AsyncSession, user_id: uuid.UUID, e: GoogleEdit, block: Block
) -> list[StagedMutation]:
    """The same constraint shape PATCH /blocks produces (edit → `pin`, delete → `forbid` on
    the old span; a pin the block realizes is removed first), sourced `system` (the user acted
    in Google, not in the app; mutation_source has no better value)."""
    if e.kind == "move":
        assert e.new_span is not None
        kind, span = "pin", e.new_span
    else:
        kind, span = "forbid", e.old_span
    realized = (
        await tx.execute(
            select(BlockPin.id)
            .where(
                BlockPin.user_id == user_id,
                BlockPin.kind == "pin",
                BlockPin.project_id == block.project_id,
                BlockPin.span == block.span,
            )
            .order_by(BlockPin.id)
        )
    ).scalars()

    def system(kind_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind_,
            "source": "system",
            "interpretation": e.interpretation,
            "payload": payload,
        }

    raw = [system("pin_removed", {"pin_id": str(pin_id)}) for pin_id in realized]
    raw.append(
        system(
            "pin_added",
            {
                "pin_id": str(uuid.uuid4()),
                "project_id": str(block.project_id),
                "span": _span_json(span),
                "kind": kind,
                "created_from_block_id": str(block.id),
            },
        )
    )
    return parse_staged(raw)


async def _live_row(
    tx: AsyncSession, user_id: uuid.UUID, horizon: Interval, lineage_id: uuid.UUID, span: Interval
) -> Block | None:
    """The current live row of (lineage, span): earlier direct applies in the same pass mint
    new row ids, but leave every untouched block's lineage and span as they were."""
    return (
        await tx.execute(
            select(Block)
            .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
            .where(
                Block.user_id == user_id,
                WeekPlan.status == "live",
                WeekPlan.horizon == to_range(horizon),
                Block.lineage_id == lineage_id,
                Block.span == to_range(span),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _promote_direct(
    tx: AsyncSession,
    user_id: uuid.UUID,
    inp: SolverInput,
    out: SolverOutput,
    mutations: Sequence[StagedMutation],
    *,
    now: datetime,
    timezone: str,
    week_start: int,
) -> uuid.UUID:
    # Logged (review round 2, no change): several direct edits in one sync re-persist the whole
    # plan once per edit; rare, and each version stays individually verified.
    """A directly applied Google edit, with confirm's own machinery: supersede the live plan,
    persist the new one as `live` (verify() gate), inherit lineage outbox fields, apply the
    pins via `apply_mutation`, and trace `plan_confirmed` with no changeset."""
    to_supersede = await _check_supersedable(tx, user_id, inp.horizon, now)
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
    plan_id = await persist_plan(
        tx, user_id, inp, out, now=now, timezone=timezone, status="live", week_start=week_start
    )
    await _inherit_from_live(tx, plan_id, superseded, {f.lineage_id for f in inp.frozen})
    for m in mutations:
        await apply_mutation(tx, user_id, m, now=now, plan_id=plan_id)
    await _trace(
        tx,
        user_id,
        kind="plan_confirmed",
        payload={"changeset_id": None, "plan_id": str(plan_id), "reason": "google_edit"},
        source="system",
        interpretation=mutations[-1].interpretation if mutations else None,
        now=now,
        plan_id=plan_id,
    )
    return plan_id


async def _live_infeasibility(
    tx: AsyncSession, user_id: uuid.UUID, horizon: Interval
) -> Infeasibility | None:
    raw = (
        await tx.execute(
            select(WeekPlan.infeasibility).where(
                WeekPlan.user_id == user_id,
                WeekPlan.status == "live",
                WeekPlan.horizon == to_range(horizon),
            )
        )
    ).scalar_one_or_none()
    return None if raw is None else Infeasibility.model_validate(raw)


async def apply_google_edits(
    tx: AsyncSession,
    user_id: uuid.UUID,
    edits: Sequence[GoogleEdit],
    *,
    now: datetime,
    busy: Sequence[Interval],
    clock: Callable[[], datetime] | None = None,
) -> GoogleEditOutcome:
    """THE write path for edits made in Google (T4.3b; user decision "hybrid"). Runs under the
    per-user lock; never calls Google (the edits and `busy` were read before the lock).

    Edits are re-validated (`_valid_google_edit`) and processed in old-start order, each
    against the state the previous ones left:

    - move → pin on the new span (+ remove a pin the block realized), then pin + re-solve:
      (i)   every other block stays (lineage, project, span) → the re-solve goes live;
      (iii) the pin is not honoured (calendar/exception/window beat it) or the new span
            overlaps another live block → nothing applied; the pins go into the proposal;
      (ii)  otherwise → only the moved block goes live (the live plan with that one block
            moved), and the knock-on re-solve is proposed. If that plan fails verify() it is
            treated as (iii).
    - delete → forbid on the old span + the live plan without the block, applied directly;
      re-placing the lost minutes is proposed. verify() failure → treated as (iii).

    A directly applied move's row is marked `synced` with the event's new etag (it already
    matches Google, so it is not rewritten). At most ONE `google_edit` changeset is staged,
    holding every (iii) pin (possibly none: then it is the knock-on re-solve only), and only
    if no changeset is pending (the user's proposal is never expired by this: its confirm
    reconciles via the stale path) and the re-solve changes something or carries pins.

    When a changeset IS pending and an edit was applied directly with a knock-on (case ii,
    delete), the knock-on is not dropped (T4.3b review round 1): the pending changeset is
    restaged against the new live plan with the same staged list and reason, solved against
    the same fresh busy (`_restage_pending`), so the user reviews it inside their proposal.
    If it cannot be restaged it is expired, and the knock-on is staged as `google_edit`.

    `now` is the sync's instant (every solve uses it); `clock` (injected; default: `now`)
    stamps a restaged changeset at the moment of this locked write (review round 2)."""
    await lock_user(tx, user_id)
    if not edits:
        return GoogleEditOutcome()
    await sweep_past_blocks(tx, user_id, now)
    week = await _preference_week(tx, user_id)
    if week is None:
        return GoogleEditOutcome(skipped=len(edits))
    tz, week_start = week
    horizon = expected_horizon(now, tz, week_start)
    pending = await _pending(tx, user_id)

    ordered = sorted(edits, key=lambda e: (e.old_span.start, e.old_span.end, str(e.block_id)))
    valid = [e for e in ordered if await _valid_google_edit(tx, user_id, e, horizon, now)]
    skipped = len(edits) - len(valid)
    staged_all: list[StagedMutation] = []
    replaced_pins: set[uuid.UUID] = set()  # pins the direct applies removed (review round 2)
    promoted: uuid.UUID | None = None  # the last directly promoted live plan
    applied = staged_edits = 0
    need_proposal = False

    for e in valid:
        block = await _live_row(tx, user_id, horizon, e.lineage_id, e.old_span)
        if block is None:  # pragma: no cover - defensive: an earlier edit took it away
            skipped += 1
            continue
        mutations = await _google_edit_mutations(tx, user_id, e, block)
        inp = await build_solver_input(tx, user_id, horizon, now, tz, staged=mutations, busy=busy)
        previous = await load_previous(tx, user_id, horizon, now)
        lineage = str(e.lineage_id)
        others = [b for b in previous if not (b.lineage_id == lineage and b.span == e.old_span)]
        direct: SolverOutput | None = None
        if e.kind == "delete":
            direct = SolverOutput(
                input_schema_version=inp.input_schema_version,
                solver_version=inp.solver_version,
                blocks=tuple(_with_pin_flags(others, inp)),
                infeasibility=await _live_infeasibility(tx, user_id, horizon),
            )
        else:
            assert e.new_span is not None
            out = pack(inp, previous)
            moved = PlannedBlock(
                lineage_id=lineage, project_id=str(block.project_id), span=e.new_span, pinned=True
            )
            realized = moved in out.blocks
            rest = [b for b in out.blocks if b != moved]
            occupied = [*(b.span for b in others), *(f.span for f in inp.frozen)]
            if realized and _block_key(rest) == _block_key(others):
                direct = out  # (i)
            elif realized and not any(e.new_span.overlaps(s) for s in occupied):
                direct = SolverOutput(  # (ii): only the moved block
                    input_schema_version=inp.input_schema_version,
                    solver_version=inp.solver_version,
                    blocks=tuple(_with_pin_flags([*others, moved], inp)),
                    infeasibility=await _live_infeasibility(tx, user_id, horizon),
                )
                need_proposal = True
        if direct is not None and verify(list(direct.blocks), inp):
            direct = None  # the one-block change is not a valid plan: review it instead
        if direct is None:  # (iii)
            staged_all.extend(mutations)
            staged_edits += 1
            need_proposal = True
            continue
        if e.kind == "delete":
            need_proposal = True
        plan_id = promoted = await _promote_direct(
            tx, user_id, inp, direct, mutations, now=now, timezone=tz, week_start=week_start
        )
        replaced_pins.update(m.payload.pin_id for m in mutations if isinstance(m, PinRemoved))
        if e.kind == "move" and e.new_span is not None:
            await tx.execute(
                update(Block)
                .where(
                    Block.plan_id == plan_id,
                    Block.lineage_id == e.lineage_id,
                    Block.span == to_range(e.new_span),
                )
                .values(gcal_status="synced", gcal_event_id=e.event_id, gcal_etag=e.new_etag)
            )
        applied += 1

    changeset_id: uuid.UUID | None = None
    if need_proposal and pending and applied:
        restage = await _restage_pending(
            tx,
            user_id,
            pending[-1],
            now=now,
            busy=busy,
            replaced_pins=replaced_pins,
            stamp=now if clock is None else clock(),
        )
        changeset_id = restage.changeset_id
        if restage.status == "expired":
            pending = await _pending(tx, user_id)  # the knock-on is staged below if none left
    if need_proposal and pending:
        skipped += staged_edits  # (iii) pins are not staged: the next sync re-evaluates them
        staged_edits = 0
    elif need_proposal:
        inp = await build_solver_input(tx, user_id, horizon, now, tz, staged=staged_all, busy=busy)
        previous = await load_previous(tx, user_id, horizon, now)
        out = pack(inp, previous)
        if staged_all or _schedule_key([(b.project_id, b.span) for b in out.blocks]) != (
            _schedule_key([(b.project_id, b.span) for b in previous])
        ):
            changeset_id = await stage_changeset(
                tx,
                user_id,
                staged_all,
                inp,
                out,
                now=now,
                timezone=tz,
                source="system",
                reason="google_edit",
            )
        elif promoted is not None:
            # No proposal: the follow-up solve reproduces the promoted plan, so its
            # infeasibility is the solver's verdict on exactly that plan (rule 3; M4 gate). A
            # delete that took a project's last slot surfaces its new demand_shortfall here
            # instead of inheriting the previous plan's verdict.
            await tx.execute(
                update(WeekPlan)
                .where(WeekPlan.id == promoted, WeekPlan.status == "live")
                .values(
                    infeasibility=None
                    if out.infeasibility is None
                    else out.infeasibility.model_dump(mode="json")
                )
            )
    return GoogleEditOutcome(
        applied=applied, staged=staged_edits, skipped=skipped, changeset_id=changeset_id
    )


@dataclass(frozen=True)
class _Restage:
    status: Literal["restaged", "expired", "kept"]
    changeset_id: uuid.UUID | None = None


async def _restage_pending(
    tx: AsyncSession,
    user_id: uuid.UUID,
    cs: Changeset,
    *,
    now: datetime,
    busy: Sequence[Interval],
    replaced_pins: set[uuid.UUID],
    stamp: datetime,
) -> _Restage:
    """Re-solve the user's pending changeset on the CURRENT state (the new live plan) with its
    own staged list and reason, like the `changeset_stale` restage (app.api.changesets
    `_restage`, whose fallback is exactly this `propose_changeset`). Same session and
    transaction; the per-user lock is already held (re-entrant). D4 expires the old one;
    staged projects it creates are re-pointed, not deleted (O9 carry-forward).

    Review round 2 (main-session decision):
    - `pin_removed` entries for pins the Google edit just removed/replaced (`replaced_pins`)
      are dropped; the rest of the list is kept;
    - if that empties the list, or the rest still cannot apply (another missing pin, or
      staging refuses it, in a savepoint), the pending changeset is EXPIRED, never left
      actionable but unconfirmable ("expired"; the caller then stages the knock-on);
    - the replacement's `created_at` is `max(stamp, replaced + 1µs)`: `stamp` is the clock
      at the locked write, so a changeset staged while the sync awaited Google is never
      replaced by an older-looking one (clients drop older replacements);
    - a list that moves the week (week_start change) is "kept": `busy` does not cover that
      solve, and its confirm re-fetches busy time."""
    original = parse_staged(cs.staged)
    staged = [
        m for m in original if not (isinstance(m, PinRemoved) and m.payload.pin_id in replaced_pins)
    ]
    if await solve_horizon(tx, user_id, staged, now) != await solve_horizon(tx, user_id, [], now):
        return _Restage("kept")

    async def expire() -> _Restage:
        await _close_changeset(tx, user_id, cs, "expired", keep_projects=(), now=stamp)
        return _Restage("expired")

    if original and not staged:
        return await expire()
    removed = sorted(m.payload.pin_id for m in staged if isinstance(m, PinRemoved))
    if removed:
        existing = set(
            (
                await tx.execute(
                    select(BlockPin.id).where(BlockPin.user_id == user_id, BlockPin.id.in_(removed))
                )
            ).scalars()
        )
        if existing != set(removed):  # `overlay` tolerates it, but confirm would answer 404
            return await expire()
    created_at = max(stamp, cs.created_at + timedelta(microseconds=1))
    try:
        async with tx.begin_nested():
            new_id = await propose_changeset(
                tx,
                user_id,
                staged,
                now=now,
                busy=busy,
                source=staged[0].source if staged else "system",
                reason=cs.reason,  # type: ignore[arg-type]
                created_at=created_at,
            )
    except (InvalidMutation, PlanVerificationError, PreferencesNotSet):
        return await expire()
    return _Restage("restaged", new_id)


async def propose_drift(
    tx: AsyncSession, user_id: uuid.UUID, *, now: datetime, busy: Sequence[Interval]
) -> uuid.UUID | None:
    """Drift (ARCH §3 "Busy time & drift"; T4.3b): a live block that has not started overlaps
    FRESH busy time (read before the lock) → propose a re-solve, `reason: drift`, nothing
    staged. None when there is no drift, no preferences/plan, or a changeset is already
    pending (that one is returned by /gcal/sync instead; its confirm re-checks busy)."""
    await lock_user(tx, user_id)
    if await _pending(tx, user_id):
        return None
    week = await _preference_week(tx, user_id)
    if week is None:
        return None
    horizon = expected_horizon(now, *week)
    fresh = clip_intervals(busy, horizon)
    previous = await load_previous(tx, user_id, horizon, now)
    if not any(iv.overlaps(b.span) for b in previous for iv in fresh):
        return None
    return await propose_changeset(
        tx, user_id, [], now=now, horizon=horizon, busy=busy, source="system", reason="drift"
    )

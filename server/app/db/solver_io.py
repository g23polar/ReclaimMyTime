"""DB state ⇄ solver contract (ARCHITECTURE.md §3 "Time", §4).

Local wall-clock → absolute projection happens HERE, before the solver boundary: windows and
preferences are local + IANA tz, everything handed to `app.scheduler` is an absolute UTC
Interval. `app.scheduler` never sees a timezone.

DST policy for a local wall-clock time (applies to windows, week bounds and deadlines):
- ambiguous (fall-back fold): the EARLIER instant (fold=0);
- nonexistent (spring-forward gap): the first valid instant after the gap.
A window whose both ends fall in one gap projects to nothing.

Layout: pure helpers (projection, overlay, `to_solver_input`) are unit-testable without a
database; `build_solver_input` loads state through the caller's transaction and calls them.
Nothing here reads the clock — `now` is always injected.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AvailabilityException, Block, BlockPin, Project, UserPreferences, WeekPlan
from app.db.models.staged import (
    DAY_KEYS,
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
    hhmm_to_minute,
)
from app.scheduler import (
    SOLVER_VERSION,
    ExceptionInput,
    FrozenBlock,
    Interval,
    Pin,
    PlannedBlock,
    ProjectInput,
    SolverInput,
)


class PreferencesNotSet(LookupError):
    """No preferences row (and none staged): no solve is possible yet (onboarding)."""


# ---------------------------------------------------------------- tstzrange ⇄ Interval


def to_range(iv: Interval) -> Range[datetime]:
    return Range(iv.start, iv.end, bounds="[)")


def to_interval(r: Range[datetime]) -> Interval:
    assert r.lower is not None and r.upper is not None, "unbounded tstzrange"
    return Interval(start=r.lower, end=r.upper)


# ---------------------------------------------------------------- local → absolute


def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def local_to_utc(day: date, minute_of_day: int, tz: ZoneInfo) -> datetime:
    """The absolute instant of local `day` + `minute_of_day` (whole days roll over, so
    1440 is the next local midnight)."""
    if not 0 <= minute_of_day:
        raise ValueError("minute_of_day must be >= 0")
    day += timedelta(days=minute_of_day // 1440)  # 1440 = next local midnight
    minute_of_day %= 1440
    naive = datetime.combine(day, time(minute_of_day // 60, minute_of_day % 60))
    # Walk forward minute by minute until the wall time exists (gaps are at most a few hours).
    for step in range(24 * 60):
        cand = naive + timedelta(minutes=step)
        instant = cand.replace(tzinfo=tz).astimezone(UTC)  # fold=0: earlier of an ambiguous pair
        if instant.astimezone(tz).replace(tzinfo=None) == cand:
            return instant
    raise ValueError(f"no valid local time at or after {naive} in {tz.key}")


def week_horizon(week_start: date, tz: str) -> Interval:
    """Local midnight of `week_start` to local midnight 7 days later, as absolute time.
    A DST week is 167 or 169 hours long."""
    zi = zone(tz)
    return Interval(
        start=local_to_utc(week_start, 0, zi), end=local_to_utc(week_start, 7 * 1440, zi)
    )


def week_start_for(now: datetime, tz: str, week_start: int) -> date:
    """The local date of the week (ISO weekday `week_start`, 1 = Monday) containing `now`."""
    local = now.astimezone(zone(tz)).date()
    return local - timedelta(days=(local.isoweekday() - week_start) % 7)


def deadline_instant(target_date: date, tz: str) -> datetime:
    """End of the local `target_date` (ARCH §3: "end of that local day")."""
    return local_to_utc(target_date + timedelta(days=1), 0, zone(tz))


def project_windows(
    windows: Mapping[str, Any] | None, horizon: Interval, tz: str
) -> list[Interval]:
    """Project a `{"mon": [["08:00","22:00"]], ...}` map onto absolute intervals inside
    `horizon`, sorted by start. Windows are clipped to the horizon."""
    if not windows:
        return []
    zi = zone(tz)
    first = horizon.start.astimezone(zi).date() - timedelta(days=1)
    last = horizon.end.astimezone(zi).date()
    out: list[Interval] = []
    day = first
    while day <= last:
        for start_s, end_s in windows.get(DAY_KEYS[day.weekday()], []):
            start = max(local_to_utc(day, hhmm_to_minute(start_s), zi), horizon.start)
            end = min(local_to_utc(day, hhmm_to_minute(end_s), zi), horizon.end)
            if start < end:
                out.append(Interval(start=start, end=end))
        day += timedelta(days=1)
    return sorted(out, key=lambda iv: (iv.start, iv.end))


def hours_to_minutes(hours: Decimal | float) -> int:
    """numeric(·,1) hours → integer minutes (0.1 h = 6 min, so this is exact)."""
    minutes = Decimal(str(hours)) * 60
    if minutes != minutes.to_integral_value():
        raise ValueError(f"{hours} h is not a whole number of minutes")
    return int(minutes)


# ---------------------------------------------------------------- constraint state + overlay


@dataclass(frozen=True)
class ProjectState:
    id: str
    kind: Literal["deadline", "perennial"]
    status: str
    max_block_minutes: int
    min_block_minutes: int
    target_date: date | None = None
    estimated_total_hours: Decimal | None = None
    weekly_target_hours: Decimal | None = None
    preferred_windows: Mapping[str, Any] | None = None
    preferred_strict: bool = False
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreferencesState:
    timezone: str
    schedulable_windows: Mapping[str, Any]
    day_diversity: Literal["focused", "mixed"]
    buffer_minutes: int
    week_start: int


@dataclass(frozen=True)
class ExceptionState:
    id: str
    span: Interval
    blocks_all: bool
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PinState:
    id: str
    kind: Literal["pin", "forbid"]
    span: Interval
    project_id: str | None
    created_from_block_id: str | None


@dataclass(frozen=True)
class ConstraintState:
    """Scheduling inputs. `projects` holds non-archived, non-staged rows (any of active /
    paused / done) so a staged status change can be overlaid; only `active` ones are solved."""

    preferences: PreferencesState | None
    projects: Mapping[str, ProjectState] = field(default_factory=dict)
    exceptions: Mapping[str, ExceptionState] = field(default_factory=dict)
    pins: Mapping[str, PinState] = field(default_factory=dict)


def _dec(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def project_state_from_draft(m: ProjectCreated) -> ProjectState:
    d = m.payload.project
    return ProjectState(
        id=str(m.payload.project_id),
        kind=d.kind,
        status="active",
        max_block_minutes=d.max_block_minutes,
        min_block_minutes=d.effective_min_block_minutes,
        target_date=getattr(d, "target_date", None),
        estimated_total_hours=_dec(getattr(d, "estimated_total_hours", None)),
        weekly_target_hours=_dec(getattr(d, "weekly_target_hours", None)),
        preferred_windows=d.preferred_windows,
        preferred_strict=d.preferred_strict,
        requirements=tuple(d.requirements),
    )


def patch_project(p: ProjectState, m: ProjectUpdated) -> ProjectState:
    """Apply a ProjectPatch to a project; raises InvalidMutation on a kind mismatch or a
    min/max inversion (the DB CHECKs would also catch these, later and less clearly)."""
    sent = m.payload.changes.sent()
    other_kind = (
        {"weekly_target_hours"}
        if p.kind == "deadline"
        else {"target_date", "estimated_total_hours"}
    )
    bad = sorted(other_kind & sent.keys())
    if bad:
        raise InvalidMutation(
            "validation_failed", f"{', '.join(bad)} not valid for a {p.kind} project"
        )
    changes: dict[str, Any] = {}
    for name, value in sent.items():
        if name == "name":
            continue  # not a scheduling input
        if name in ("estimated_total_hours", "weekly_target_hours"):
            value = _dec(value)
        if name == "requirements":
            value = tuple(value)
        changes[name] = value
    out = replace(p, **changes)
    if out.min_block_minutes > out.max_block_minutes:
        raise InvalidMutation("validation_failed", "min_block_minutes must be <= max_block_minutes")
    return out


def overlay(state: ConstraintState, staged: Sequence[StagedMutation]) -> ConstraintState:
    """The hypothetical state after `staged` (pure; nothing is written).

    Strict where the result would otherwise be wrong (creating an existing id, updating an
    archived or unknown project); lenient on removals of ids not loaded here, which
    `apply_mutation` re-checks against the database at confirm.
    """
    prefs = state.preferences
    projects = dict(state.projects)
    exceptions = dict(state.exceptions)
    pins = dict(state.pins)
    for m in staged:
        if isinstance(m, ProjectCreated):
            pid = str(m.payload.project_id)
            if pid in projects:
                raise InvalidMutation("project_exists", f"project {pid} already exists")
            projects[pid] = project_state_from_draft(m)
        elif isinstance(m, ProjectUpdated):
            pid = str(m.payload.project_id)
            if pid not in projects:
                raise InvalidMutation("not_found", f"project {pid} is unknown or archived")
            projects[pid] = patch_project(projects[pid], m)
        elif isinstance(m, ProjectArchived):
            projects.pop(str(m.payload.project_id), None)
        elif isinstance(m, PreferencesUpdated):
            p = m.payload.preferences
            prefs = PreferencesState(
                timezone=p.timezone,
                schedulable_windows=p.schedulable_windows,
                day_diversity=p.day_diversity,
                buffer_minutes=p.buffer_minutes,
                week_start=p.week_start,
            )
        elif isinstance(m, ExceptionAdded):
            e = m.payload
            if str(e.exception_id) in exceptions:
                raise InvalidMutation("exception_exists", f"exception {e.exception_id} exists")
            exceptions[str(e.exception_id)] = ExceptionState(
                id=str(e.exception_id),
                span=Interval(start=e.span.start, end=e.span.end),
                blocks_all=e.blocks_all,
                tags=tuple(e.blocks_tags),
            )
        elif isinstance(m, ExceptionRemoved):
            exceptions.pop(str(m.payload.exception_id), None)
        elif isinstance(m, PinAdded):
            pa = m.payload
            if str(pa.pin_id) in pins:
                raise InvalidMutation("pin_exists", f"pin {pa.pin_id} exists")
            pins[str(pa.pin_id)] = PinState(
                id=str(pa.pin_id),
                kind=pa.kind,
                span=Interval(start=pa.span.start, end=pa.span.end),
                project_id=None if pa.project_id is None else str(pa.project_id),
                created_from_block_id=(
                    None if pa.created_from_block_id is None else str(pa.created_from_block_id)
                ),
            )
        elif isinstance(m, PinRemoved):
            pins.pop(str(m.payload.pin_id), None)
    return ConstraintState(preferences=prefs, projects=projects, exceptions=exceptions, pins=pins)


def to_solver_input(
    state: ConstraintState,
    *,
    horizon: Interval,
    now: datetime,
    tz: str,
    frozen: Sequence[FrozenBlock] = (),
    completed_minutes: Mapping[str, int] | None = None,
    lineage_by_block: Mapping[str, str] | None = None,
    busy: Sequence[Interval] = (),
) -> SolverInput:
    """Assemble the SolverInput (pure). Only `active` projects are solved; pins and
    exceptions are kept when they overlap the horizon and have not ended by `now`."""
    prefs = state.preferences
    if prefs is None:
        raise PreferencesNotSet("no preferences: nothing to solve against")
    completed = completed_minutes or {}
    lineages = lineage_by_block or {}

    def live(span: Interval) -> bool:
        return span.overlaps(horizon) and span.end > now

    projects: list[ProjectInput] = []
    for p in sorted(state.projects.values(), key=lambda p: p.id):
        if p.status != "active":
            continue
        common: dict[str, Any] = {
            "id": p.id,
            "max_block_minutes": p.max_block_minutes,
            "min_block_minutes": p.min_block_minutes,
            "preferred_windows": tuple(project_windows(p.preferred_windows, horizon, tz)),
            "preferred_windows_strict": p.preferred_strict,
            "requirement_tags": tuple(p.requirements),
        }
        if p.kind == "deadline":
            assert p.estimated_total_hours is not None and p.target_date is not None
            total = hours_to_minutes(p.estimated_total_hours)
            projects.append(
                ProjectInput(
                    **common,
                    remaining_minutes=max(0, total - completed.get(p.id, 0)),
                    target_date=p.target_date,
                    deadline=deadline_instant(p.target_date, tz),
                )
            )
        else:
            assert p.weekly_target_hours is not None
            projects.append(
                ProjectInput(
                    **common,
                    remaining_minutes=0,
                    weekly_target_minutes=hours_to_minutes(p.weekly_target_hours),
                )
            )

    return SolverInput(
        solver_version=SOLVER_VERSION,
        now=now,
        horizon=horizon,
        busy=tuple(busy),
        schedulable=tuple(project_windows(prefs.schedulable_windows, horizon, tz)),
        frozen=tuple(frozen),
        pins=tuple(
            Pin(
                id=p.id,
                kind=p.kind,
                span=p.span,
                project_id=p.project_id,
                created_from_lineage_id=(
                    lineages.get(p.created_from_block_id) if p.created_from_block_id else None
                ),
            )
            for p in sorted(state.pins.values(), key=lambda p: p.id)
            if live(p.span)
        ),
        projects=tuple(projects),
        exceptions=tuple(
            ExceptionInput(id=e.id, span=e.span, tags=e.tags, blocks_all=e.blocks_all)
            for e in sorted(state.exceptions.values(), key=lambda e: e.id)
            if live(e.span)
        ),
        day_diversity=prefs.day_diversity,
        buffer_minutes=prefs.buffer_minutes,
    )


# ---------------------------------------------------------------- DB loaders


async def load_constraint_state(tx: AsyncSession, user_id: uuid.UUID) -> ConstraintState:
    """Committed scheduling state. Staged projects are excluded (O1/D23): they enter a
    solve only through the staged list being solved."""
    prefs_row = (
        await tx.execute(
            select(UserPreferences)
            .where(UserPreferences.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    prefs = (
        None
        if prefs_row is None
        else PreferencesState(
            timezone=prefs_row.timezone,
            schedulable_windows=prefs_row.schedulable_windows,
            day_diversity=prefs_row.day_diversity,  # type: ignore[arg-type]
            buffer_minutes=prefs_row.buffer_minutes,
            week_start=prefs_row.week_start,
        )
    )
    project_rows = (
        await tx.execute(
            select(Project)
            .where(Project.user_id == user_id, Project.status.in_(("active", "paused", "done")))
            .execution_options(populate_existing=True)
        )
    ).scalars()
    projects = {
        str(r.id): ProjectState(
            id=str(r.id),
            kind=r.kind,  # type: ignore[arg-type]
            status=r.status,
            max_block_minutes=r.max_block_minutes,
            min_block_minutes=r.min_block_minutes,
            target_date=r.target_date,
            estimated_total_hours=r.estimated_total_hours,
            weekly_target_hours=r.weekly_target_hours,
            preferred_windows=r.preferred_windows,
            preferred_strict=r.preferred_strict,
            requirements=tuple(r.requirements),
        )
        for r in project_rows
    }
    exc_rows = (
        await tx.execute(
            select(AvailabilityException)
            .where(AvailabilityException.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    exceptions = {
        str(r.id): ExceptionState(
            id=str(r.id),
            span=to_interval(r.span),
            blocks_all=r.blocks_all,
            tags=tuple(r.blocks_tags),
        )
        for r in exc_rows
    }
    pin_rows = (
        await tx.execute(
            select(BlockPin)
            .where(BlockPin.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    pins = {
        str(r.id): PinState(
            id=str(r.id),
            kind=r.kind,  # type: ignore[arg-type]
            span=to_interval(r.span),
            project_id=None if r.project_id is None else str(r.project_id),
            created_from_block_id=(
                None if r.created_from_block_id is None else str(r.created_from_block_id)
            ),
        )
        for r in pin_rows
    }
    return ConstraintState(preferences=prefs, projects=projects, exceptions=exceptions, pins=pins)


def _live_blocks(user_id: uuid.UUID, horizon: Interval) -> Any:
    return (
        select(Block)
        .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
        .where(
            WeekPlan.user_id == user_id,
            WeekPlan.status == "live",
            WeekPlan.horizon.overlaps(to_range(horizon)),
            Block.span.overlaps(to_range(horizon)),
        )
        .order_by(func.lower(Block.span), Block.lineage_id)
        .execution_options(populate_existing=True)
    )


async def load_frozen(
    tx: AsyncSession, user_id: uuid.UUID, horizon: Interval, now: datetime
) -> list[FrozenBlock]:
    """Past and in-progress blocks (start < now) of the live plan(s) over `horizon`."""
    rows = (await tx.execute(_live_blocks(user_id, horizon))).scalars()
    return [
        FrozenBlock(
            lineage_id=str(b.lineage_id),
            project_id=str(b.project_id),
            span=to_interval(b.span),
            state=b.state,  # type: ignore[arg-type]
        )
        for b in rows
        if b.span.lower is not None and b.span.lower < now
    ]


async def load_previous(
    tx: AsyncSession, user_id: uuid.UUID, horizon: Interval, now: datetime
) -> list[PlannedBlock]:
    """Not-yet-started blocks of the live plan: `pack(previous=...)` for lineage reuse."""
    rows = (await tx.execute(_live_blocks(user_id, horizon))).scalars()
    return [
        PlannedBlock(
            lineage_id=str(b.lineage_id), project_id=str(b.project_id), span=to_interval(b.span)
        )
        for b in rows
        if b.span.lower is not None and b.span.lower >= now
    ]


async def completed_minutes(tx: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Σ completed block minutes per project (ARCH §3: remaining effort is computed, never
    stored), over live AND superseded plans so effort outside a replacement horizon (e.g. a
    week-start change) is never lost.

    Frozen copy-forward repeats a block verbatim in every later version, so rows are
    de-duplicated by (lineage_id, span), keeping the authoritative row: the live plan's,
    else the newest superseded one (its state reflects later complete/skip edits). Span is
    part of the key because pack mints lineages as uuid5(project:ordinal:salt), so two
    different weeks can share a lineage id.
    """
    rank = (
        func.row_number()
        .over(
            partition_by=(Block.lineage_id, Block.span),
            order_by=(
                case((WeekPlan.status == "live", 0), else_=1),
                WeekPlan.created_at.desc(),
                WeekPlan.version.desc(),
                WeekPlan.id,
            ),
        )
        .label("rank")
    )
    latest = (
        select(Block.project_id, Block.state, Block.span, rank)
        .join(WeekPlan, and_(WeekPlan.id == Block.plan_id, WeekPlan.user_id == Block.user_id))
        .where(Block.user_id == user_id, WeekPlan.status.in_(("live", "superseded")))
        .subquery()
    )
    minutes = func.sum(
        func.extract("epoch", func.upper(latest.c.span) - func.lower(latest.c.span)) / 60
    )
    rows = await tx.execute(
        select(latest.c.project_id, minutes)
        .where(latest.c.rank == 1, latest.c.state == "completed")
        .group_by(latest.c.project_id)
    )
    return {str(pid): int(total) for pid, total in rows.all()}


async def lineages_for_blocks(
    tx: AsyncSession, user_id: uuid.UUID, block_ids: Iterable[str]
) -> dict[str, str]:
    """block id → lineage id (pin provenance → Pin.created_from_lineage_id)."""
    ids = sorted({uuid.UUID(b) for b in block_ids})
    if not ids:
        return {}
    rows = await tx.execute(
        select(Block.id, Block.lineage_id).where(Block.user_id == user_id, Block.id.in_(ids))
    )
    return {str(bid): str(lid) for bid, lid in rows.all()}


async def build_solver_input(
    tx: AsyncSession,
    user_id: uuid.UUID,
    horizon: Interval,
    now: datetime,
    tz: str | None = None,
    *,
    staged: Sequence[StagedMutation] = (),
    busy: Sequence[Interval] = (),
) -> SolverInput:
    """SolverInput for `user_id` over `horizon`, from committed state plus the hypothetical
    `staged` mutations (the changeset being solved). `tz` defaults to the (overlaid)
    preferences timezone (contracts D10: windows and target dates use preferences.timezone).

    `busy`: TODO(T4.2) — the caller passes freeBusy intervals (app calendar excluded); this
    module never talks to Google. Until T4.2 lands every solve runs with busy=[].
    """
    state = overlay(await load_constraint_state(tx, user_id), staged)
    if state.preferences is None:
        raise PreferencesNotSet("no preferences: nothing to solve against")
    tz = tz or state.preferences.timezone
    provenance = [p.created_from_block_id for p in state.pins.values() if p.created_from_block_id]
    return to_solver_input(
        state,
        horizon=horizon,
        now=now,
        tz=tz,
        frozen=await load_frozen(tx, user_id, horizon, now),
        completed_minutes=await completed_minutes(tx, user_id),
        lineage_by_block=await lineages_for_blocks(tx, user_id, provenance),
        busy=busy,
    )


# ---------------------------------------------------------------- SolverOutput → block rows


@dataclass(frozen=True)
class SourceBlock:
    """The fields of an existing block row that copy forward with its lineage."""

    state: str
    gcal_status: str
    gcal_event_id: str | None
    gcal_etag: str | None


def block_rows(
    plan_id: uuid.UUID,
    user_id: uuid.UUID,
    solver_input: SolverInput,
    planned: Sequence[PlannedBlock],
    sources: Mapping[str, SourceBlock],
) -> list[dict[str, Any]]:
    """Insert values for a plan's `blocks` (pure).

    Frozen blocks are copied verbatim (lineage, project, span, state) and keep their gcal
    outbox fields from `sources` (keyed by lineage). New blocks start `planned` with
    `gcal_status='pending'`; confirm later inherits event ids for reused lineages.
    """
    rows: list[dict[str, Any]] = []
    for f in sorted(solver_input.frozen, key=lambda f: (f.span.start, f.lineage_id)):
        src = sources.get(f.lineage_id)
        rows.append(
            {
                "plan_id": plan_id,
                "user_id": user_id,
                "project_id": uuid.UUID(f.project_id),
                "lineage_id": uuid.UUID(f.lineage_id),
                "span": to_range(f.span),
                "state": f.state,
                "gcal_status": src.gcal_status if src else "pending",
                "gcal_event_id": src.gcal_event_id if src else None,
                "gcal_etag": src.gcal_etag if src else None,
            }
        )
    for b in sorted(planned, key=lambda b: (b.span.start, b.lineage_id)):
        rows.append(
            {
                "plan_id": plan_id,
                "user_id": user_id,
                "project_id": uuid.UUID(b.project_id),
                "lineage_id": uuid.UUID(b.lineage_id),
                "span": to_range(b.span),
                "state": "planned",
                "gcal_status": "pending",
                "gcal_event_id": None,
                "gcal_etag": None,
            }
        )
    return rows

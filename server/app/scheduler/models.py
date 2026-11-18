"""Solver contract models (ARCHITECTURE.md §4).

Pure data: no clock, environment, or timezone-database reads. Every span is an
absolute, timezone-aware datetime pair; projection from local wall-clock time
happens before the solver boundary. Every accepted datetime is normalized to a
UTC instant on validation, so all duration/overlap/buffer math is instant math
(no DST geometry). Interval endpoints are whole minutes, so every duration is an
integer number of minutes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

INPUT_SCHEMA_VERSION: Literal[1] = 1

# Integer minutes. Strict: floats, bools, and numeric strings are rejected.
Minutes = Annotated[int, Field(strict=True, ge=0)]
PositiveMinutes = Annotated[int, Field(strict=True, gt=0)]


def _exact_int(v: object) -> object:
    # Literal[1] alone would accept True (bool == 1); require a real int.
    if type(v) is not int:
        raise ValueError("schema version must be the integer 1")
    return v


SchemaVersion = Annotated[Literal[1], BeforeValidator(_exact_int)]


def _to_utc(v: datetime) -> datetime:
    return v.astimezone(UTC)


# An aware datetime, normalized to the same instant in UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

# TODO(ARCH §4): identifier types are unspecified. The DDL uses uuid; the solver
# treats ids as opaque strings and the DB layer converts at the boundary.
Id = Annotated[str, Field(min_length=1)]

ViolationKind = Literal[
    "before_now",
    "block_length",
    "buffer",
    "busy",
    "duplicate_lineage",
    "exception",
    "forbid",
    "horizon",
    "overlap",
    "perennial_daily",
    "pin_missing",
    "pin_unmatched",
    "preferred_window",
    "unknown_project",
    "window",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(values)))


class Interval(_Model):
    """Half-open absolute span [start, end) of whole-minute UTC instants."""

    start: UtcDatetime
    end: UtcDatetime

    @field_validator("start", "end")
    @classmethod
    def _whole_minute(cls, v: datetime) -> datetime:
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("Interval endpoints must be whole minutes")
        return v

    @model_validator(mode="after")
    def _start_before_end(self) -> Interval:
        if not self.start < self.end:
            raise ValueError("Interval requires start < end")
        return self

    @property
    def duration_minutes(self) -> int:
        return (self.end - self.start) // timedelta(minutes=1)

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: Interval) -> bool:
        return self.start <= other.start and other.end <= self.end


class FrozenBlock(_Model):
    """A past or in-progress block, copied verbatim into the next plan."""

    lineage_id: Id
    project_id: Id
    span: Interval
    state: Literal["planned", "completed", "skipped"]


class Pin(_Model):
    """A user constraint: `pin` = this project occupies this span; `forbid` = it must not."""

    id: Id
    kind: Literal["pin", "forbid"]
    span: Interval
    project_id: Id | None = None  # None only for a blanket forbid
    # Lineage of the block whose manual edit created this pin; the pinned block keeps it.
    created_from_lineage_id: Id | None = None

    @model_validator(mode="after")
    def _pin_has_project(self) -> Pin:
        if self.kind == "pin" and self.project_id is None:
            raise ValueError("a 'pin' requires project_id; only 'forbid' may be blanket")
        return self


class ProjectInput(_Model):
    id: Id
    remaining_minutes: Minutes
    weekly_target_minutes: Minutes | None = None
    max_block_minutes: PositiveMinutes
    min_block_minutes: PositiveMinutes
    preferred_windows: tuple[Interval, ...] = ()
    preferred_windows_strict: bool = False
    requirement_tags: tuple[str, ...] = ()
    # Local target date of a deadline project (contracts D10); used only to phrase the
    # extend_deadline proposal. Plain date arithmetic only, no tz math.
    target_date: date | None = None
    # Absolute instant the work is due: the end of `target_date`, projected through the user's
    # tz by the caller. Bounds placement and drives deadline pressure (ARCH §4, 2026-11-14).
    deadline: UtcDatetime | None = None

    @field_validator("requirement_tags")
    @classmethod
    def _normalize_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)

    @model_validator(mode="after")
    def _min_le_max(self) -> ProjectInput:
        if self.min_block_minutes > self.max_block_minutes:
            raise ValueError("min_block_minutes must be <= max_block_minutes")
        return self


class ExceptionInput(_Model):
    """An availability exception: blocks everything, or only projects needing a tag."""

    id: Id
    span: Interval
    tags: tuple[str, ...] = ()
    blocks_all: bool = False

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)

    @model_validator(mode="after")
    def _blocks_something(self) -> ExceptionInput:
        if not self.blocks_all and not self.tags:
            raise ValueError("an exception must set blocks_all or list at least one tag")
        return self


class SolverInput(_Model):
    input_schema_version: SchemaVersion = INPUT_SCHEMA_VERSION
    solver_version: Annotated[str, Field(min_length=1)]
    now: UtcDatetime  # injected — the solver never reads a clock
    horizon: Interval
    busy: tuple[Interval, ...] = ()
    schedulable: tuple[Interval, ...] = ()
    frozen: tuple[FrozenBlock, ...] = ()
    pins: tuple[Pin, ...] = ()
    projects: tuple[ProjectInput, ...] = ()
    exceptions: tuple[ExceptionInput, ...] = ()
    day_diversity: Literal["focused", "mixed"]
    buffer_minutes: Minutes

    @model_validator(mode="after")
    def _unique_project_ids(self) -> SolverInput:
        ids = [p.id for p in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("project ids must be unique")
        return self


class PlannedBlock(_Model):
    lineage_id: Id  # reused when a block is "the same session, moved"
    project_id: Id
    span: Interval
    pinned: bool = False


class Violation(_Model):
    kind: ViolationKind
    message: str
    block_lineage_ids: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()

    @field_validator("block_lineage_ids", "related_ids")
    @classmethod
    def _normalize_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)


RelaxationKind = Literal[
    "extend_deadline",
    "reduce_estimate",
    "reduce_weekly_target",
    "raise_max_block_minutes",
    "pause_project",
    "remove_pin",
    "remove_exception",
]

# The kind-specific fields each relaxation kind requires (contracts D17); all others stay None.
_RELAXATION_FIELDS: dict[str, frozenset[str]] = {
    "extend_deadline": frozenset({"project_id", "target_date"}),
    "reduce_estimate": frozenset({"project_id", "remaining_minutes"}),
    "reduce_weekly_target": frozenset({"project_id", "weekly_target_minutes"}),
    "raise_max_block_minutes": frozenset({"project_id", "max_block_minutes"}),
    "pause_project": frozenset({"project_id"}),
    "remove_pin": frozenset({"pin_id"}),
    "remove_exception": frozenset({"exception_id"}),
}
_RELAXATION_OPTIONAL = (
    "project_id",
    "pin_id",
    "exception_id",
    "target_date",
    "remaining_minutes",
    "weekly_target_minutes",
    "max_block_minutes",
)


class Relaxation(_Model):
    """A candidate trade-off the user may pick (PRD Principle 2), never applied automatically.

    Proposed values are in solver units (integer minutes, local date). The API layer maps
    them onto the D17 PATCH body: remaining_minutes -> estimated_total_hours (adding the
    project's completed minutes), weekly_target_minutes -> weekly_target_hours.
    """

    kind: RelaxationKind
    description: str
    affected_ids: tuple[str, ...] = ()
    recovers_minutes: PositiveMinutes  # shortfall minutes this relaxation would eliminate
    project_id: Id | None = None
    pin_id: Id | None = None
    exception_id: Id | None = None
    target_date: date | None = None  # extend_deadline: proposed new target date
    remaining_minutes: PositiveMinutes | None = None  # reduce_estimate: proposed remaining
    weekly_target_minutes: PositiveMinutes | None = None  # reduce_weekly_target
    max_block_minutes: PositiveMinutes | None = None  # raise_max_block_minutes

    @model_validator(mode="after")
    def _kind_fields(self) -> Relaxation:
        required = _RELAXATION_FIELDS[self.kind]
        for name in _RELAXATION_OPTIONAL:
            present = getattr(self, name) is not None
            if present != (name in required):
                state = "requires" if name in required else "does not take"
                raise ValueError(f"relaxation {self.kind} {state} {name}")
        return self


PinConflict = Literal[
    "busy",
    "exception",
    "forbid",
    "window",
    "horizon",
    "before_now",
    "preferred_window",
    "overlap",
    "buffer",
]


class PinInvalidated(_Model):
    """A pin that verify() rejects; reported, never dropped (ARCH §3 conflict order)."""

    kind: Literal["pin_invalidated"] = "pin_invalidated"
    pin_id: Id
    pin_kind: Literal["pin", "forbid"]
    project_id: Id | None
    span: Interval
    conflicts_with: PinConflict
    exception_id: Id | None = None  # set when conflicts_with == "exception"
    related_lineage_ids: tuple[str, ...] = ()  # other blocks, for overlap/buffer

    @field_validator("related_lineage_ids")
    @classmethod
    def _normalize_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)


class DemandShortfall(_Model):
    """A project's required minutes in this horizon could not all be placed."""

    kind: Literal["demand_shortfall"] = "demand_shortfall"
    project_id: Id
    required_minutes: Minutes
    scheduled_minutes: Minutes
    shortfall_minutes: PositiveMinutes

    @model_validator(mode="after")
    def _consistent(self) -> DemandShortfall:
        if self.required_minutes - self.scheduled_minutes != self.shortfall_minutes:
            raise ValueError("shortfall_minutes must equal required - scheduled")
        return self


InfeasibilityViolation = Annotated[DemandShortfall | PinInvalidated, Field(discriminator="kind")]


class Infeasibility(_Model):
    violations: tuple[InfeasibilityViolation, ...] = ()
    relaxations: tuple[Relaxation, ...] = ()
    # Raw verify() output behind the violations above (pin conflicts, pin_missing), so the
    # mapping onto D17 kinds loses nothing.
    verify_violations: tuple[Violation, ...] = ()


class SolverOutput(_Model):
    input_schema_version: SchemaVersion = INPUT_SCHEMA_VERSION
    solver_version: Annotated[str, Field(min_length=1)]
    blocks: tuple[PlannedBlock, ...] = ()
    infeasibility: Infeasibility | None = None

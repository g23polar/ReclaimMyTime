"""Solver contract models (ARCHITECTURE.md §4).

Pure data: no clock, environment, or timezone-database reads. Every span is an
absolute, timezone-aware datetime pair; projection from local wall-clock time
happens before the solver boundary. Every accepted datetime is normalized to a
UTC instant on validation, so all duration/overlap/buffer math is instant math
(no DST geometry). Interval endpoints are whole minutes, so every duration is an
integer number of minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


class Relaxation(_Model):
    # TODO(ARCH §4 / contracts D17): the relaxation kind enum and typed parameters
    # are a proposal for T1.2 to confirm; kept as an open string until then.
    kind: str
    description: str
    affected_ids: tuple[str, ...] = ()


class Infeasibility(_Model):
    # TODO(ARCH §4 / contracts D17): D17 specifies demand_shortfall / pin_invalidated
    # violations for the API; mapping verify() Violations onto them is T1.2's job.
    violations: tuple[Violation, ...] = ()
    relaxations: tuple[Relaxation, ...] = ()


class SolverOutput(_Model):
    input_schema_version: SchemaVersion = INPUT_SCHEMA_VERSION
    solver_version: Annotated[str, Field(min_length=1)]
    blocks: tuple[PlannedBlock, ...] = ()
    infeasibility: Infeasibility | None = None

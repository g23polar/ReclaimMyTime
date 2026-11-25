"""Typed shape of `changesets.staged` (contracts/openapi.yaml `StagedMutation`).

Pydantic, not SQLAlchemy: this is the schema of a jsonb column, shared by
`app.db.write_paths` (apply/stage/confirm) and `app.db.solver_io` (hypothetical overlay).
Payloads are server-resolved: ids pre-assigned, spans absolute, windows local wall-clock.
Request-level validation (422 shapes) is T2.3's; these models re-check what the write paths
rely on so a bad payload can never reach the database half-applied.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^(?:([01]\d|2[0-3]):([0-5]\d)|(24):(00))$")

MutationSource = Literal["agent", "manual", "system"]


class InvalidMutation(ValueError):
    """A staged mutation that cannot be applied to the current state (T2.3 maps to 409/422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


LimitResource = Literal["projects", "exceptions", "pins"]


class LimitReached(InvalidMutation):
    """Issue #24: a write that would add a row past a per-user cap (409 `limit_reached`)."""

    def __init__(self, resource: LimitResource, limit: int) -> None:
        super().__init__("limit_reached", _LIMIT_MESSAGES[resource].format(limit=limit))
        self.resource = resource
        self.limit = limit


_LIMIT_MESSAGES: dict[str, str] = {
    "projects": "You can have at most {limit} projects. Archive one to add another.",
    "exceptions": "You can have at most {limit} upcoming exceptions. Remove one to add another.",
    "pins": (
        "You can have at most {limit} upcoming pins and blocked times. Remove one to add another."
    ),
}


# ---------------------------------------------------------------- windows


def hhmm_to_minute(value: str) -> int:
    """'HH:MM' → minute of day; '24:00' → 1440 (valid only as a window end)."""
    m = _HHMM.match(value)
    if not m:
        raise ValueError(f"time {value!r} is not HH:MM")
    if m.group(3):
        return 1440
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_week_windows(raw: Mapping[str, Any]) -> dict[str, list[list[str]]]:
    """Check a `{"mon": [["08:00","22:00"]], ...}` map (contracts NOTES "Time"): keys mon..sun,
    `[start, end)` local HH:MM, 24:00 only as an end, no midnight crossing, sorted and
    non-overlapping within a day. Returns a plain-JSON copy."""
    if not isinstance(raw, Mapping):
        raise ValueError("windows must be an object keyed mon..sun")
    out: dict[str, list[list[str]]] = {}
    for key, windows in raw.items():
        if key not in DAY_KEYS:
            raise ValueError(f"unknown day key {key!r}")
        if not isinstance(windows, Sequence) or isinstance(windows, str):
            raise ValueError(f"{key}: windows must be a list")
        prev_end = -1
        day: list[list[str]] = []
        for w in windows:
            if not isinstance(w, Sequence) or isinstance(w, str) or len(w) != 2:
                raise ValueError(f"{key}: each window is [start, end]")
            start, end = str(w[0]), str(w[1])
            s, e = hhmm_to_minute(start), hhmm_to_minute(end)
            if s == 1440:
                raise ValueError(f"{key}: 24:00 is allowed only as an end")
            if not s < e:
                raise ValueError(f"{key}: window {start}-{end} must have start < end")
            if s < prev_end:
                raise ValueError(f"{key}: windows must be sorted and non-overlapping")
            prev_end = e
            day.append([start, end])
        out[key] = day
    return out


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{value!r} is not an IANA timezone") from exc
    return value


WeekWindows = Annotated[dict[str, list[list[str]]], Field()]
Tag = Annotated[str, Field(min_length=1, max_length=40)]
MAX_USER_TAGS = 50  # issue #7: size of one user's `user_tags` vocabulary
# issue #24: rows in use per user (`check_row_caps`)
MAX_USER_PROJECTS = 100
MAX_USER_EXCEPTIONS = 200
MAX_USER_PINS = 200

# Write-side caps (issue #4), enforced by the request models (`app.api.schemas`) and the agent
# tool drafts, not here: stored rows over a cap must still parse.
MAX_TAGS = 20
MAX_WINDOWS_PER_DAY = 10


def _unique_tags(v: list[str]) -> list[str]:
    if len(set(v)) != len(v):
        raise ValueError("tags must be unique")
    return v


def _hours(v: float, upper: float) -> float:
    r = round(v, 1)  # D19: the server rounds to 0.1
    if not 0 < r <= upper:
        raise ValueError(f"hours must be > 0 and <= {upper}")
    return r


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Span(_Model):
    """tstzrange `[start, end)` as UTC whole minutes (the solver's Interval grid)."""

    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def _utc_minutes(cls, v: datetime) -> datetime:
        if v.second or v.microsecond:
            raise ValueError("span endpoints must be whole minutes")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if not self.start < self.end:
            raise ValueError("span requires start < end")
        return self


class _DraftBase(_Model):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    max_block_minutes: Annotated[int, Field(strict=True, ge=1)]
    min_block_minutes: Annotated[int, Field(strict=True, ge=1)] | None = None
    preferred_windows: WeekWindows | None = None
    preferred_strict: bool = False
    requirements: list[Tag] = []

    @field_validator("preferred_windows")
    @classmethod
    def _windows(cls, v: dict[str, Any] | None) -> dict[str, list[list[str]]] | None:
        return None if v is None else validate_week_windows(v)

    @field_validator("requirements")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _unique_tags(v)

    @model_validator(mode="after")
    def _min_le_max(self) -> _DraftBase:
        if self.min_block_minutes is not None and self.min_block_minutes > self.max_block_minutes:
            raise ValueError("min_block_minutes must be <= max_block_minutes")
        return self

    @property
    def effective_min_block_minutes(self) -> int:
        # ARCH §3: no DB default; the app sets LEAST(25, max_block_minutes).
        if self.min_block_minutes is not None:
            return self.min_block_minutes
        return min(25, self.max_block_minutes)


class DeadlineProjectDraft(_DraftBase):
    kind: Literal["deadline"]
    target_date: date
    estimated_total_hours: float

    @field_validator("estimated_total_hours")
    @classmethod
    def _est(cls, v: float) -> float:
        return _hours(v, 99999.9)


class PerennialProjectDraft(_DraftBase):
    kind: Literal["perennial"]
    weekly_target_hours: float

    @field_validator("weekly_target_hours")
    @classmethod
    def _weekly(cls, v: float) -> float:
        return _hours(v, 168)


ProjectDraft = Annotated[DeadlineProjectDraft | PerennialProjectDraft, Field(discriminator="kind")]


class ProjectPatch(_Model):
    """Partial update; which fields were *sent* matters (`preferred_windows: null` clears)."""

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    target_date: date | None = None
    estimated_total_hours: float | None = None
    weekly_target_hours: float | None = None
    max_block_minutes: Annotated[int, Field(strict=True, ge=1)] | None = None
    min_block_minutes: Annotated[int, Field(strict=True, ge=1)] | None = None
    preferred_windows: WeekWindows | None = None
    preferred_strict: bool | None = None
    requirements: list[Tag] | None = None
    status: Literal["active", "paused", "done"] | None = None

    @field_validator("preferred_windows")
    @classmethod
    def _windows(cls, v: dict[str, Any] | None) -> dict[str, list[list[str]]] | None:
        return None if v is None else validate_week_windows(v)

    @field_validator("requirements")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _unique_tags(v)

    @field_validator("estimated_total_hours")
    @classmethod
    def _est(cls, v: float | None) -> float | None:
        return None if v is None else _hours(v, 99999.9)

    @field_validator("weekly_target_hours")
    @classmethod
    def _weekly(cls, v: float | None) -> float | None:
        return None if v is None else _hours(v, 168)

    @model_validator(mode="after")
    def _non_empty(self) -> ProjectPatch:
        if not self.model_fields_set:
            raise ValueError("a project patch must change at least one field")
        nullable = {"preferred_windows"}
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def sent(self) -> dict[str, Any]:
        """Only the fields the caller sent, as Python values."""
        return {name: getattr(self, name) for name in sorted(self.model_fields_set)}


class Preferences(_Model):
    timezone: str
    schedulable_windows: WeekWindows
    day_diversity: Literal["focused", "mixed"]
    buffer_minutes: Annotated[int, Field(strict=True, ge=0, le=240)]
    week_start: Annotated[int, Field(strict=True, ge=1, le=7)]

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("schedulable_windows")
    @classmethod
    def _windows(cls, v: dict[str, Any]) -> dict[str, list[list[str]]]:
        return validate_week_windows(v)


# ---------------------------------------------------------------- payloads


class ProjectCreatedPayload(_Model):
    project_id: uuid.UUID
    project: ProjectDraft


class ProjectUpdatedPayload(_Model):
    project_id: uuid.UUID
    changes: ProjectPatch

    @field_serializer("changes")
    def _only_sent(self, v: ProjectPatch) -> dict[str, Any]:
        return v.model_dump(mode="json", exclude_unset=True)


class ProjectArchivedPayload(_Model):
    project_id: uuid.UUID


class PreferencesUpdatedPayload(_Model):
    preferences: Preferences


class ExceptionAddedPayload(_Model):
    exception_id: uuid.UUID
    span: Span
    blocks_all: bool
    blocks_tags: list[Tag]
    reason: str | None

    @field_validator("blocks_tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _unique_tags(v)

    @model_validator(mode="after")
    def _blocks_something(self) -> ExceptionAddedPayload:
        # Mirrors the DDL CHECK: a forgotten tag list is an error, not a blackout.
        if not self.blocks_all and not self.blocks_tags:
            raise ValueError("an exception must set blocks_all or list at least one tag")
        return self


class ExceptionRemovedPayload(_Model):
    exception_id: uuid.UUID


class PinAddedPayload(_Model):
    pin_id: uuid.UUID
    project_id: uuid.UUID | None
    span: Span
    kind: Literal["pin", "forbid"]
    created_from_block_id: uuid.UUID | None

    @model_validator(mode="after")
    def _pin_has_project(self) -> PinAddedPayload:
        if self.kind == "pin" and self.project_id is None:
            raise ValueError("a 'pin' requires project_id; only 'forbid' may be blanket")
        return self


class PinRemovedPayload(_Model):
    pin_id: uuid.UUID


class _Staged(_Model):
    source: MutationSource
    interpretation: str | None


class ProjectCreated(_Staged):
    kind: Literal["project_created"] = "project_created"
    payload: ProjectCreatedPayload


class ProjectUpdated(_Staged):
    kind: Literal["project_updated"] = "project_updated"
    payload: ProjectUpdatedPayload


class ProjectArchived(_Staged):
    kind: Literal["project_archived"] = "project_archived"
    payload: ProjectArchivedPayload


class PreferencesUpdated(_Staged):
    kind: Literal["preferences_updated"] = "preferences_updated"
    payload: PreferencesUpdatedPayload


class ExceptionAdded(_Staged):
    kind: Literal["exception_added"] = "exception_added"
    payload: ExceptionAddedPayload


class ExceptionRemoved(_Staged):
    kind: Literal["exception_removed"] = "exception_removed"
    payload: ExceptionRemovedPayload


class PinAdded(_Staged):
    kind: Literal["pin_added"] = "pin_added"
    payload: PinAddedPayload


class PinRemoved(_Staged):
    kind: Literal["pin_removed"] = "pin_removed"
    payload: PinRemovedPayload


StagedMutation = Annotated[
    ProjectCreated
    | ProjectUpdated
    | ProjectArchived
    | PreferencesUpdated
    | ExceptionAdded
    | ExceptionRemoved
    | PinAdded
    | PinRemoved,
    Field(discriminator="kind"),
]

_STAGED_LIST: TypeAdapter[list[StagedMutation]] = TypeAdapter(list[StagedMutation])


def parse_staged(raw: Sequence[Any]) -> list[StagedMutation]:
    """Validate a `changesets.staged` list (models or plain JSON dicts)."""
    items = [m.model_dump(mode="json") if isinstance(m, BaseModel) else m for m in raw]
    return _STAGED_LIST.validate_python(items)


def dump_staged(staged: Sequence[StagedMutation]) -> list[dict[str, Any]]:
    """The jsonb value stored in `changesets.staged`."""
    return [m.model_dump(mode="json") for m in staged]


def created_project_ids(staged: Sequence[StagedMutation]) -> list[uuid.UUID]:
    """Project ids created by `project_created` entries, in list order, without duplicates."""
    ids: list[uuid.UUID] = []
    for m in staged:
        if isinstance(m, ProjectCreated) and m.payload.project_id not in ids:
            ids.append(m.payload.project_id)
    return ids


def staged_tags(staged: Sequence[StagedMutation]) -> list[str]:
    """Tags a list would add to the vocabulary once confirmed (project requirements and
    exception `blocks_tags`), in first-seen order, without duplicates."""
    tags: list[str] = []
    for m in staged:
        if isinstance(m, ProjectCreated):
            tags.extend(m.payload.project.requirements)
        elif isinstance(m, ProjectUpdated) and m.payload.changes.requirements is not None:
            tags.extend(m.payload.changes.requirements)
        elif isinstance(m, ExceptionAdded):
            tags.extend(m.payload.blocks_tags)
    return list(dict.fromkeys(tags))


def check_row_caps(
    *,
    projects: Iterable[uuid.UUID],
    exceptions: Iterable[uuid.UUID],
    pins: Iterable[uuid.UUID],
    staged: Sequence[StagedMutation],
    now: datetime,
) -> None:
    """Issue #24: refuse a list that adds a row past a per-user cap. `projects` / `exceptions`
    / `pins` are the committed rows in use (projects neither archived nor staged; exceptions
    and pins whose span ends after `now`). The list is judged on its net result: archives and
    removals of in-use rows count down, adds that are still upcoming count up, and Google
    edits (`system` pins) never count. A kind is refused only when the list adds a counted
    row of it and ends above the cap, so updates, archives and removals always pass. Raises
    `LimitReached` for the first such kind (projects, exceptions, pins)."""
    counted: dict[LimitResource, set[uuid.UUID]] = {
        "projects": set(projects),
        "exceptions": set(exceptions),
        "pins": set(pins),
    }
    adds: set[LimitResource] = set()

    def add(resource: LimitResource, ident: uuid.UUID) -> None:
        if ident not in counted[resource]:
            counted[resource].add(ident)
            adds.add(resource)

    for m in staged:
        if isinstance(m, ProjectArchived):
            counted["projects"].discard(m.payload.project_id)
        elif isinstance(m, ExceptionRemoved):
            counted["exceptions"].discard(m.payload.exception_id)
        elif isinstance(m, PinRemoved):
            counted["pins"].discard(m.payload.pin_id)
        elif isinstance(m, ProjectCreated):
            add("projects", m.payload.project_id)
        elif isinstance(m, ExceptionAdded) and m.payload.span.end > now:
            add("exceptions", m.payload.exception_id)
        elif isinstance(m, PinAdded) and m.payload.span.end > now and m.source != "system":
            add("pins", m.payload.pin_id)
    limits: dict[LimitResource, int] = {
        "projects": MAX_USER_PROJECTS,
        "exceptions": MAX_USER_EXCEPTIONS,
        "pins": MAX_USER_PINS,
    }
    for resource, limit in limits.items():
        if resource in adds and len(counted[resource]) > limit:
            raise LimitReached(resource, limit)

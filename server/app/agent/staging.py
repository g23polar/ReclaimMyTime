"""Per-turn changeset assembly for the agent (ARCH §8 write discipline).

A `TurnStaging` starts from the pending changeset's staged list (decision 2026-11-15: every
agent turn builds on it, so multi-turn onboarding accumulates) and applies the model's
`stage_*` / `discard_staged` tool calls in memory. Nothing here touches the database; T3.2
hands `staged` to `propose_changeset` when the turn proposes.

Each call resolves per-turn labels to ids, converts local dates/times to absolute spans in
the user's timezone, enforces the tag vocabulary, and re-runs `overlay` on the candidate
list so a mutation the write paths would reject is refused *now*, as a tool error the model
can correct — never half-applied.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.schemas import (
    TOOL_INPUTS,
    DiscardStaged,
    LocalTime,
    StageExceptionAdd,
    StageExceptionRemove,
    StagePinAdd,
    StagePinRemove,
    StagePreferences,
    StageProjectArchive,
    StageProjectCreate,
    StageProjectUpdate,
    WeekWindows,
    unwire,
)
from app.db.models.staged import (
    DAY_KEYS,
    MAX_TAGS,
    ExceptionRemoved,
    InvalidMutation,
    PinAdded,
    PinRemoved,
    ProjectArchived,
    ProjectCreated,
    ProjectUpdated,
    StagedMutation,
    hhmm_to_minute,
    parse_staged,
)
from app.db.solver_io import ConstraintState, local_to_utc, overlay, zone


class ToolError(Exception):
    """A tool call the server refused; the message goes back to the model as an error
    `tool_result` so it can correct the call or ask the user."""


def normalize_tag(tag: str) -> str:
    """Comparison key for near-duplicate tags: case, spaces, `-` and `_` are not significant."""
    return re.sub(r"[\s_-]+", "-", tag.strip().lower()).strip("-")


@dataclass(frozen=True)
class TurnInputs:
    """Committed state for one turn, loaded by the handler. Label order is the order of the
    id sequences (the handler sorts them by creation time)."""

    committed: ConstraintState
    project_ids: Sequence[str]
    exception_ids: Sequence[str]
    pin_ids: Sequence[str]
    vocabulary: Sequence[str]
    pending: Sequence[StagedMutation]
    timezone: str  # committed preferences' timezone, else the request's X-Timezone


class TurnStaging:
    def __init__(self, inputs: TurnInputs, *, new_id: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        self._committed = inputs.committed
        self._timezone = inputs.timezone
        self._new_id = new_id
        self._vocabulary = list(inputs.vocabulary)
        self._next = {"P": 1, "E": 1, "N": 1, "S": 1}
        self.projects: dict[str, str] = {}
        self.exceptions: dict[str, str] = {}
        self.pins: dict[str, str] = {}
        self.staged: list[tuple[str, StagedMutation]] = []
        self.changed = False
        for pid in inputs.project_ids:
            self.projects[self._label("P")] = pid
        for eid in inputs.exception_ids:
            self.exceptions[self._label("E")] = eid
        for nid in inputs.pin_ids:
            self.pins[self._label("N")] = nid
        for m in inputs.pending:
            self._append(m)
        overlay(self._committed, self.mutations)  # the pending list must still apply

    # ------------------------------------------------------------ public

    @property
    def mutations(self) -> list[StagedMutation]:
        return [m for _, m in self.staged]

    def state(self) -> ConstraintState:
        """The hypothetical state: committed + everything staged so far."""
        return overlay(self._committed, self.mutations)

    def vocabulary(self) -> list[str]:
        """Existing tags plus tags introduced by staged projects, in first-seen order."""
        tags = list(self._vocabulary)
        for m in self.mutations:
            if isinstance(m, ProjectCreated):
                tags.extend(m.payload.project.requirements)
            elif isinstance(m, ProjectUpdated) and m.payload.changes.requirements is not None:
                tags.extend(m.payload.changes.requirements)
        return list(dict.fromkeys(tags))

    def call(self, name: str, raw: Mapping[str, Any]) -> str:
        """Apply one tool call; returns the text for a successful `tool_result`.
        Raises ToolError for anything the model should correct."""
        entry = TOOL_INPUTS.get(name)
        if entry is None or name == "propose_schedule":
            raise ToolError(f"unknown staging tool {name!r}")
        if _bad_text(raw):
            # Postgres rejects NUL and unpaired surrogates; refuse before any write path.
            raise ToolError("text fields must be valid UTF-8 without NUL characters")
        try:
            wired = unwire(name, raw)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        try:
            draft = entry[0].model_validate(wired)
        except ValidationError as exc:
            raise ToolError(_describe(exc)) from None
        handler: Callable[[Any], str] = getattr(self, f"_{name}")
        try:
            return handler(draft)
        except InvalidMutation as exc:
            raise ToolError(exc.message) from None
        except ValidationError as exc:
            raise ToolError(_describe(exc)) from None

    # ------------------------------------------------------------ tools

    def _stage_project_create(self, d: StageProjectCreate) -> str:
        if d.kind == "deadline":
            _require(d, "target_date", "estimated_total_hours")
            _forbid(d, "weekly_target_hours")
        else:
            _require(d, "weekly_target_hours")
            _forbid(d, "target_date", "estimated_total_hours")
        tags = self._tags(d.requirements, d.new_tags)
        project: dict[str, Any] = {
            "kind": d.kind,
            "name": d.name,
            "max_block_minutes": d.max_block_minutes,
            "min_block_minutes": d.min_block_minutes,
            "preferred_windows": _windows(d.preferred_windows),
            "preferred_strict": d.preferred_strict,
            "requirements": tags,
        }
        if d.kind == "deadline":
            project |= {
                "target_date": d.target_date.isoformat() if d.target_date else None,
                "estimated_total_hours": d.estimated_total_hours,
            }
        else:
            project["weekly_target_hours"] = d.weekly_target_hours
        pid = self._new_id()
        label = self._stage(
            "project_created",
            {"project_id": str(pid), "project": project},
            _interpretation(d.interpretation, d.new_tags),
        )
        plabel = self._label("P")
        self.projects[plabel] = str(pid)
        return f"Staged {label}: new project {plabel} {d.name!r}."

    def _stage_project_update(self, d: StageProjectUpdate) -> str:
        pid = self._resolve(self.projects, d.project, "project")
        changes: dict[str, Any] = {}
        for field in (
            "name",
            "estimated_total_hours",
            "weekly_target_hours",
            "max_block_minutes",
            "min_block_minutes",
            "preferred_strict",
            "status",
        ):
            value = getattr(d, field)
            if value is not None:
                changes[field] = value
        if d.target_date is not None:
            changes["target_date"] = d.target_date.isoformat()
        if d.clear_preferred_windows:
            if d.preferred_windows is not None:
                raise ToolError("set preferred_windows or clear_preferred_windows, not both")
            changes["preferred_windows"] = None
        elif d.preferred_windows is not None:
            changes["preferred_windows"] = _windows(d.preferred_windows)
        if d.requirements is not None or d.new_tags:
            if d.requirements is not None:
                base = list(d.requirements)
            elif pid in (projects := self.state().projects):
                base = list(projects[pid].requirements)
            else:
                raise ToolError(f"{d.project} is archived")
            changes["requirements"] = self._tags(base, d.new_tags)
        if not changes:
            raise ToolError("nothing to change: set at least one field")
        label = self._stage(
            "project_updated",
            {"project_id": pid, "changes": changes},
            _interpretation(d.interpretation, d.new_tags),
        )
        return f"Staged {label}: update to {d.project}."

    def _stage_project_archive(self, d: StageProjectArchive) -> str:
        pid = self._resolve(self.projects, d.project, "project")
        label = self._stage("project_archived", {"project_id": pid}, d.interpretation)
        return f"Staged {label}: archive {d.project}."

    def _stage_preferences(self, d: StagePreferences) -> str:
        current = self.state().preferences
        values: dict[str, Any] = {
            "schedulable_windows": _windows(d.schedulable_windows),
            "day_diversity": d.day_diversity,
            "buffer_minutes": d.buffer_minutes,
            "week_start": d.week_start,
        }
        for field, value in list(values.items()):
            if value is None:
                if current is None:
                    raise ToolError(f"{field} is required: the user has no preferences yet")
                values[field] = getattr(current, field)
        timezone = current.timezone if current is not None else self._timezone
        label = self._stage(
            "preferences_updated",
            {"preferences": {"timezone": timezone, **values}},
            d.interpretation,
        )
        return f"Staged {label}: preferences."

    def _stage_exception_add(self, d: StageExceptionAdd) -> str:
        vocabulary = self.vocabulary()
        unknown = [t for t in d.blocks_tags if t not in vocabulary]
        if unknown:
            raise ToolError(f"unknown tags {unknown}; known tags: {vocabulary}")
        eid = self._new_id()
        label = self._stage(
            "exception_added",
            {
                "exception_id": str(eid),
                "span": self._span(d.start, d.end),
                "blocks_all": d.blocks_all,
                "blocks_tags": [] if d.blocks_all else d.blocks_tags,
                "reason": d.reason,
            },
            d.interpretation,
        )
        return f"Staged {label}: exception."

    def _stage_exception_remove(self, d: StageExceptionRemove) -> str:
        eid = self._resolve(self.exceptions, d.exception, "exception")
        label = self._stage("exception_removed", {"exception_id": eid}, d.interpretation)
        return f"Staged {label}: remove {d.exception}."

    def _stage_pin_add(self, d: StagePinAdd) -> str:
        pid = None if d.project is None else self._resolve(self.projects, d.project, "project")
        if d.kind == "pin" and pid is None:
            raise ToolError("a pin needs a project; use kind 'forbid' to keep a span free")
        nid = self._new_id()
        label = self._stage(
            "pin_added",
            {
                "pin_id": str(nid),
                "project_id": pid,
                "span": self._span(d.start, d.end),
                "kind": d.kind,
                "created_from_block_id": None,
            },
            d.interpretation,
        )
        return f"Staged {label}: {d.kind}."

    def _stage_pin_remove(self, d: StagePinRemove) -> str:
        nid = self._resolve(self.pins, d.pin, "pin")
        label = self._stage("pin_removed", {"pin_id": nid}, d.interpretation)
        return f"Staged {label}: remove {d.pin}."

    def _discard_staged(self, d: DiscardStaged) -> str:
        labels = [label for label, _ in self.staged]
        if d.staged not in labels:
            raise ToolError(f"unknown staged label {d.staged!r}; staged: {labels}")
        target = next(m for label, m in self.staged if label == d.staged)
        dropped = {d.staged}
        if isinstance(target, ProjectCreated):
            pid = target.payload.project_id
            dropped |= {label for label, m in self.staged if _targets(m) == pid}
            self.projects = {k: v for k, v in self.projects.items() if v != str(pid)}
        remaining = [(label, m) for label, m in self.staged if label not in dropped]
        overlay(self._committed, [m for _, m in remaining])
        self.staged = remaining
        self.changed = True
        return f"Discarded {', '.join(sorted(dropped, key=_label_key))}."

    # ------------------------------------------------------------ helpers

    def _label(self, prefix: str) -> str:
        n = self._next[prefix]
        self._next[prefix] = n + 1
        return f"{prefix}{n}"

    def _append(self, m: StagedMutation) -> str:
        label = self._label("S")
        self.staged.append((label, m))
        if isinstance(m, ProjectCreated):
            self.projects[self._label("P")] = str(m.payload.project_id)
        return label

    def _stage(self, kind: str, payload: dict[str, Any], interpretation: str) -> str:
        """Validate `payload` as a staged mutation, check it applies on top of everything
        staged so far, then record it. Raises before recording anything."""
        raw = {"kind": kind, "source": "agent", "interpretation": interpretation}
        m = parse_staged([{**raw, "payload": payload}])[0]
        overlay(self._committed, [*self.mutations, m])
        label = self._label("S")
        self.staged.append((label, m))
        self.changed = True
        return label

    def _resolve(self, labels: Mapping[str, str], label: str, what: str) -> str:
        """Label → id. A row already archived/removed by a staged entry no longer resolves:
        a second removal would pass `overlay` (lenient on removals) but fail at confirm,
        making the whole changeset unconfirmable."""
        if label not in labels:
            known = sorted(labels, key=_label_key)
            raise ToolError(f"unknown {what} label {label!r}; known: {known}")
        ident = labels[label]
        for slabel, m in self.staged:
            if _removes(m) == ident:
                raise ToolError(f"{label} is already removed by {slabel}")
        return ident

    def _tags(self, requirements: Sequence[str], new_tags: Sequence[str]) -> list[str]:
        vocabulary = self.vocabulary()
        unknown = [t for t in requirements if t not in vocabulary]
        if unknown:
            raise ToolError(
                f"unknown tags {unknown}; known tags: {vocabulary}. A new tag goes in "
                "new_tags, and only after the user agreed to create it."
            )
        by_key = {normalize_tag(t): t for t in vocabulary}
        out = list(requirements)
        for tag in new_tags:
            existing = by_key.setdefault(normalize_tag(tag), tag)  # covers same-call additions
            if existing != tag:
                raise ToolError(f"tag {tag!r} duplicates tag {existing!r}; use that")
            out.append(tag)
        tags = list(dict.fromkeys(out))
        if len(tags) > MAX_TAGS:
            raise ToolError(f"requirements: at most {MAX_TAGS} tags per project, got {len(tags)}")
        return tags

    def _span(self, start: LocalTime, end: LocalTime) -> dict[str, str]:
        prefs = self.state().preferences
        tz = zone(prefs.timezone if prefs is not None else self._timezone)
        return {
            "start": local_to_utc(start.day, hhmm_to_minute(start.time), tz).isoformat(),
            "end": local_to_utc(end.day, hhmm_to_minute(end.time), tz).isoformat(),
        }


def _targets(m: StagedMutation) -> uuid.UUID | None:
    """The project a staged mutation depends on, if any."""
    if isinstance(m, ProjectUpdated | ProjectArchived):
        return m.payload.project_id
    if isinstance(m, PinAdded):
        return m.payload.project_id
    return None


def _bad_text(value: Any) -> bool:
    if isinstance(value, str):
        if "\x00" in value:
            return True
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, Mapping):
        return any(_bad_text(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_bad_text(v) for v in value)
    return False


def removed_ids(staged: Sequence[StagedMutation]) -> dict[str, int]:
    """Committed row id → index in `staged` of the archive/removal that takes it away."""
    return {rid: i for i, m in enumerate(staged) if (rid := _removes(m)) is not None}


def _removes(m: StagedMutation) -> str | None:
    """The committed row id a staged archive/removal takes away, if any."""
    if isinstance(m, ProjectArchived):
        return str(m.payload.project_id)
    if isinstance(m, ExceptionRemoved):
        return str(m.payload.exception_id)
    if isinstance(m, PinRemoved):
        return str(m.payload.pin_id)
    return None


def _label_key(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]))


def _windows(w: WeekWindows | None) -> dict[str, list[list[str]]] | None:
    if w is None:
        return None
    return {
        day: [[x.start, x.end] for x in sorted(getattr(w, day), key=lambda x: x.start)]
        for day in DAY_KEYS
    }


def _interpretation(text: str, new_tags: Sequence[str]) -> str:
    # The confirmation card must show tags the user is about to create (ARCH §5).
    return f"{text} (new tags: {', '.join(new_tags)})" if new_tags else text


def _require(d: BaseModel, *fields: str) -> None:
    missing = [f for f in fields if getattr(d, f) is None]
    if missing:
        raise ToolError(f"{', '.join(missing)} required for this kind of project")


def _forbid(d: BaseModel, *fields: str) -> None:
    present = [f for f in fields if getattr(d, f) is not None]
    if present:
        raise ToolError(f"{', '.join(present)} must be null for this kind of project")


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in err["loc"]) or "input"
        parts.append(f"{loc}: {err['msg']}")
    return "invalid input: " + "; ".join(parts)

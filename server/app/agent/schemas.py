"""LLM-facing tool inputs (ARCH §5, §8) and their strict Anthropic tool definitions.

These are *drafts*, not staged mutations: the model writes local dates and `HH:MM` wall-clock
times only, and refers to existing rows by per-turn labels (`P1`, `E1`, `N1`, `S1`), never by
UUID. `app.agent.staging` resolves labels, applies the user's timezone and turns a draft into
an `app.db.models.staged` mutation.

The tool definitions are static — byte-identical for every user and turn — so the cached
prompt prefix is shared and the tool list never changes inside a turn (a changed prefix
invalidates replayed thinking blocks). Label and tag membership is therefore checked by the
server, not by the schema. Every field is required (strict tool use); "no value" is `null`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Keywords strict tool use does not support. Pydantic still enforces them on validation.
_UNSUPPORTED = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "default",
        "title",
    }
)

HHMM = r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$"
Label = str  # e.g. "P1"; resolved by app.agent.staging against the turn's label map

INTERPRETATION = (
    "One short sentence restating what you understood, in the user's terms "
    "(shown on the confirmation card)."
)


class _Draft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Window(_Draft):
    start: str = Field(pattern=HHMM, description="Local HH:MM, inclusive.")
    end: str = Field(pattern=HHMM, description="Local HH:MM, exclusive; 24:00 = midnight.")


class WeekWindows(_Draft):
    """Local wall-clock windows per weekday; an empty list means none that day."""

    mon: list[Window]
    tue: list[Window]
    wed: list[Window]
    thu: list[Window]
    fri: list[Window]
    sat: list[Window]
    sun: list[Window]


class LocalTime(_Draft):
    day: date = Field(description="Local calendar date, YYYY-MM-DD.")
    time: str = Field(pattern=HHMM, description="Local HH:MM; 24:00 = end of that day.")


class StageProjectCreate(_Draft):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["deadline", "perennial"] = Field(
        description="deadline = a total amount of work due by a date; perennial = an "
        "ongoing weekly habit with no end date."
    )
    target_date: date | None = Field(description="Deadline projects only: the due date.")
    estimated_total_hours: float | None = Field(
        gt=0, description="Deadline projects only: total remaining work in hours."
    )
    weekly_target_hours: float | None = Field(
        gt=0, description="Perennial projects only: hours per week."
    )
    max_block_minutes: int = Field(ge=1, description="Longest single sitting, in minutes.")
    min_block_minutes: int | None = Field(
        ge=1, description="Shortest useful sitting, in minutes; null for the default."
    )
    preferred_windows: WeekWindows | None = Field(
        description="When the user prefers to do this; null if no preference."
    )
    preferred_strict: bool = Field(
        description="true only if the user said it must happen inside the preferred windows. "
        "Strict windows replace the work hours for this project, so they may lie outside them."
    )
    requirements: list[str] = Field(
        description="Tags from the user's existing tag vocabulary (listed in the state)."
    )
    new_tags: list[str] = Field(
        description="Tags that are not in the vocabulary yet. Only after the user explicitly "
        "agreed in this conversation to create them; otherwise leave empty."
    )
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StageProjectUpdate(_Draft):
    project: Label = Field(description="Project label, e.g. P1.")
    name: str | None = Field(min_length=1, max_length=200, description="null = unchanged.")
    target_date: date | None = Field(description="Deadline projects only; null = unchanged.")
    estimated_total_hours: float | None = Field(
        gt=0, description="Deadline projects only; null = unchanged."
    )
    weekly_target_hours: float | None = Field(
        gt=0, description="Perennial projects only; null = unchanged."
    )
    max_block_minutes: int | None = Field(ge=1, description="null = unchanged.")
    min_block_minutes: int | None = Field(ge=1, description="null = unchanged.")
    preferred_windows: WeekWindows | None = Field(description="null = unchanged.")
    clear_preferred_windows: bool = Field(description="true removes the preferred windows.")
    preferred_strict: bool | None = Field(description="null = unchanged.")
    requirements: list[str] | None = Field(
        description="Replaces the tag list (existing vocabulary only); null = unchanged."
    )
    new_tags: list[str] = Field(
        description="New tags to add to the replaced list, only after the user explicitly "
        "agreed to create them; otherwise empty."
    )
    status: Literal["active", "paused", "done"] | None = Field(description="null = unchanged.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StageProjectArchive(_Draft):
    project: Label = Field(description="Project label, e.g. P1.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StagePreferences(_Draft):
    schedulable_windows: WeekWindows | None = Field(
        description="When any work may be scheduled; null = unchanged (required the first time)."
    )
    day_diversity: Literal["focused", "mixed"] | None = Field(
        description="focused = few projects per day; mixed = variety. null = unchanged "
        "(required the first time)."
    )
    buffer_minutes: int | None = Field(
        ge=0, le=240, description="Gap between blocks; null = unchanged (required the first time)."
    )
    week_start: int | None = Field(
        ge=1,
        le=7,
        description="ISO weekday the week starts on, 1 = Monday; null = unchanged "
        "(required the first time).",
    )
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StageExceptionAdd(_Draft):
    start: LocalTime
    end: LocalTime = Field(description="Exclusive end.")
    blocks_all: bool = Field(description="true = no work at all in this span.")
    blocks_tags: list[str] = Field(
        description="If blocks_all is false: the tags (existing vocabulary) that cannot be "
        "scheduled in this span."
    )
    reason: str | None = Field(max_length=200, description="Short reason, if the user gave one.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StageExceptionRemove(_Draft):
    exception: Label = Field(description="Exception label, e.g. E1.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StagePinAdd(_Draft):
    kind: Literal["pin", "forbid"] = Field(
        description="pin = this project must happen exactly in this span; forbid = keep "
        "this span free (of one project, or of everything if project is null)."
    )
    project: Label | None = Field(description="Project label; required for a pin.")
    start: LocalTime
    end: LocalTime = Field(description="Exclusive end.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class StagePinRemove(_Draft):
    pin: Label = Field(description="Pin label, e.g. N1.")
    interpretation: str = Field(min_length=1, description=INTERPRETATION)


class DiscardStaged(_Draft):
    staged: Label = Field(
        description="Staged change label, e.g. S2. Discarding a staged new project also "
        "discards the staged changes that depend on it."
    )


class ProposeSchedule(_Draft):
    pass


# ------------------------------------------------------------ wire shapes (LLM-facing)
#
# Anthropic's strict tool use caps the number of union-typed parameters per request (16; the
# drafts above have 21). The model therefore sees these shapes instead: no `null`, no
# `anyOf`. "No value" is a sentinel (`""`, `0`, `-1`, or `"unchanged"`), and a `*_set` flag
# says whether a list or window is being set. `unwire` maps them back to the drafts' `None`
# before validation, so the staging rules are unchanged.


WINDOW_FORMAT = (
    "Local windows as 'HH:MM-HH:MM' strings (end exclusive; 24:00 = midnight), e.g. "
    "'09:00-12:30'. Several per day are allowed; an empty list means none that day."
)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WireWeekWindows(_Draft):
    """`WeekWindows` as strings: far smaller in the compiled grammar (see the note above)."""

    mon: list[str] = Field(description=WINDOW_FORMAT)
    tue: list[str] = Field(description=WINDOW_FORMAT)
    wed: list[str] = Field(description=WINDOW_FORMAT)
    thu: list[str] = Field(description=WINDOW_FORMAT)
    fri: list[str] = Field(description=WINDOW_FORMAT)
    sat: list[str] = Field(description=WINDOW_FORMAT)
    sun: list[str] = Field(description=WINDOW_FORMAT)


class WireProjectCreate(_Draft):
    name: str = Field(description="Project name, 1-200 characters.")
    kind: Literal["deadline", "perennial"] = Field(
        description="deadline = a total amount of work due by a date; perennial = an "
        "ongoing weekly habit with no end date."
    )
    target_date: str = Field(
        description="Deadline projects only: due date YYYY-MM-DD. Empty string for perennial."
    )
    estimated_total_hours: float = Field(
        description="Deadline projects only: total remaining work in hours. 0 for perennial."
    )
    weekly_target_hours: float = Field(
        description="Perennial projects only: hours per week. 0 for deadline."
    )
    max_block_minutes: int = Field(description="Longest single sitting, in minutes; at least 1.")
    min_block_minutes: int = Field(
        description="Shortest useful sitting, in minutes. 0 for the default."
    )
    preferred_windows: WireWeekWindows = Field(
        description="Used only when preferred_windows_set is true; otherwise leave all days empty."
    )
    preferred_windows_set: bool = Field(
        description="true only if the user stated a time preference for this project."
    )
    preferred_strict: bool = Field(
        description="true only if the user said it must happen inside the preferred windows. "
        "Strict windows replace the work hours for this project, so they may lie outside them."
    )
    requirements: list[str] = Field(
        description="Tags from the user's existing tag vocabulary (listed in the state)."
    )
    new_tags: list[str] = Field(
        description="Tags that are not in the vocabulary yet. Only after the user explicitly "
        "agreed in this conversation to create them; otherwise leave empty."
    )
    interpretation: str = Field(description=INTERPRETATION)


class WireProjectUpdate(_Draft):
    project: str = Field(description="Project label, e.g. P1.")
    name: str = Field(description="New name, or empty string = unchanged.")
    target_date: str = Field(
        description="Deadline projects only: new due date YYYY-MM-DD; empty string = unchanged."
    )
    estimated_total_hours: float = Field(
        description="Deadline projects only: new total in hours; 0 = unchanged."
    )
    weekly_target_hours: float = Field(
        description="Perennial projects only: new hours per week; 0 = unchanged."
    )
    max_block_minutes: int = Field(description="New longest sitting; 0 = unchanged.")
    min_block_minutes: int = Field(description="New shortest sitting; 0 = unchanged.")
    preferred_windows: WireWeekWindows = Field(
        description="Used only when preferred_windows_set is true."
    )
    preferred_windows_set: bool = Field(
        description="true to replace the preferred windows with preferred_windows."
    )
    clear_preferred_windows: bool = Field(description="true removes the preferred windows.")
    preferred_strict: Literal["unchanged", "true", "false"] = Field(
        description="unchanged, or the new value as a string. Strict windows replace the work "
        "hours for this project, so they may lie outside them."
    )
    requirements: list[str] = Field(
        description="Replaces the tag list (existing vocabulary only). Used only when "
        "requirements_set is true."
    )
    requirements_set: bool = Field(description="true to replace the tag list.")
    new_tags: list[str] = Field(
        description="New tags to add to the replaced list, only after the user explicitly "
        "agreed to create them; otherwise empty."
    )
    status: Literal["unchanged", "active", "paused", "done"] = Field(
        description="unchanged, or the new status."
    )
    interpretation: str = Field(description=INTERPRETATION)


class WirePreferences(_Draft):
    schedulable_windows: WireWeekWindows = Field(
        description="Used only when schedulable_windows_set is true."
    )
    schedulable_windows_set: bool = Field(
        description="true to set when any work may be scheduled (every project). Required the "
        "first time. One project's time outside these hours belongs in its strict preferred "
        "windows, not here."
    )
    day_diversity: Literal["unchanged", "focused", "mixed"] = Field(
        description="focused = few projects per day; mixed = variety. unchanged keeps it "
        "(required the first time)."
    )
    buffer_minutes: int = Field(
        description="Gap between blocks, 0-240; -1 = unchanged (required the first time)."
    )
    week_start: int = Field(
        description="ISO weekday the week starts on, 1 = Monday, 7 = Sunday; 0 = unchanged "
        "(required the first time)."
    )
    interpretation: str = Field(description=INTERPRETATION)


class WireExceptionAdd(_Draft):
    start: LocalTime
    end: LocalTime = Field(description="Exclusive end.")
    blocks_all: bool = Field(description="true = no work at all in this span.")
    blocks_tags: list[str] = Field(
        description="If blocks_all is false: the tags (existing vocabulary) that cannot be "
        "scheduled in this span."
    )
    reason: str = Field(description="Short reason, up to 200 characters; empty if none.")
    interpretation: str = Field(description=INTERPRETATION)


class WirePinAdd(_Draft):
    kind: Literal["pin", "forbid"] = Field(
        description="pin = this project must happen exactly in this span; forbid = keep "
        "this span free (of one project, or of everything)."
    )
    project: str = Field(
        description="Project label. Required for a pin; empty string for a forbid of everything."
    )
    start: LocalTime
    end: LocalTime = Field(description="Exclusive end.")
    interpretation: str = Field(description=INTERPRETATION)


WIRE_MODELS: dict[str, type[_Draft]] = {
    "stage_project_create": WireProjectCreate,
    "stage_project_update": WireProjectUpdate,
    "stage_preferences": WirePreferences,
    "stage_exception_add": WireExceptionAdd,
    "stage_pin_add": WirePinAdd,
}

# Per tool: which fields use a sentinel, which are guarded by a `*_set` flag, and which
# enums map a sentinel-like value to a draft value (None = unchanged).
_UNWIRE: dict[str, dict[str, dict[str, Any]]] = {
    "stage_project_create": {
        "sentinels": {
            "target_date": "",
            "estimated_total_hours": 0,
            "weekly_target_hours": 0,
            "min_block_minutes": 0,
        },
        "flags": {"preferred_windows": "preferred_windows_set"},
        "enums": {},
    },
    "stage_project_update": {
        "sentinels": {
            "name": "",
            "target_date": "",
            "estimated_total_hours": 0,
            "weekly_target_hours": 0,
            "max_block_minutes": 0,
            "min_block_minutes": 0,
        },
        "flags": {
            "preferred_windows": "preferred_windows_set",
            "requirements": "requirements_set",
        },
        "enums": {
            "preferred_strict": {"unchanged": None, "true": True, "false": False},
            "status": {"unchanged": None},
        },
    },
    "stage_preferences": {
        "sentinels": {"buffer_minutes": -1, "week_start": 0},
        "flags": {"schedulable_windows": "schedulable_windows_set"},
        "enums": {"day_diversity": {"unchanged": None}},
    },
    "stage_exception_add": {"sentinels": {"reason": ""}, "flags": {}, "enums": {}},
    "stage_pin_add": {"sentinels": {"project": ""}, "flags": {}, "enums": {}},
}


# Per tool: fields holding WireWeekWindows, converted to the draft's Window objects.
_WINDOW_FIELDS: dict[str, tuple[str, ...]] = {
    "stage_project_create": ("preferred_windows",),
    "stage_project_update": ("preferred_windows",),
    "stage_preferences": ("schedulable_windows",),
}


def _parse_window(text: Any) -> dict[str, str]:
    """`'09:00-12:30'` → `{"start": "09:00", "end": "12:30"}`; raises ValueError otherwise."""
    parts = text.split("-") if isinstance(text, str) else []
    if len(parts) != 2 or not all(re.fullmatch(HHMM, p.strip()) for p in parts):
        raise ValueError(f"window {text!r} must look like 09:00-12:30 (24:00 = midnight)")
    return {"start": parts[0].strip(), "end": parts[1].strip()}


def _windows_to_draft(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    return {
        day: [
            item if isinstance(item, Mapping) else _parse_window(item)
            for item in value.get(day, [])
        ]
        for day in _DAYS
    }


def unwire(name: str, raw: Any) -> Any:
    """Map a wire-shaped tool input to the draft shape (sentinels and unset flags → None,
    window strings → Window objects). Unknown keys pass through; the draft models reject
    them. Raises ValueError for a malformed window string."""
    spec = _UNWIRE.get(name)
    if spec is None or not isinstance(raw, Mapping):
        return raw
    out = dict(raw)
    for field, flag in spec["flags"].items():
        # A missing flag means unset: the non-strict tools may omit it, and passing the field
        # through would let stray empty values wipe tags or windows (M6 gate, finding 3).
        if out.pop(flag, False) is not True:
            out[field] = None
    for field, sentinel in spec["sentinels"].items():
        value = out.get(field)
        if value is not None and not isinstance(value, bool) and value == sentinel:
            out[field] = None
    for field, mapping in spec["enums"].items():
        value = out.get(field)
        if isinstance(value, str) and value in mapping:
            out[field] = mapping[value]
    for field in _WINDOW_FIELDS.get(name, ()):
        if out.get(field) is not None:
            out[field] = _windows_to_draft(out[field])
    return out


TOOL_INPUTS: dict[str, tuple[type[_Draft], str]] = {
    "stage_project_create": (
        StageProjectCreate,
        "Stage a new project for the user's confirmation.",
    ),
    "stage_project_update": (
        StageProjectUpdate,
        "Stage a change to an existing or staged project (pause, finish, retarget, resize).",
    ),
    "stage_project_archive": (
        StageProjectArchive,
        "Stage removing a project from scheduling entirely.",
    ),
    "stage_preferences": (
        StagePreferences,
        "Stage the user's general scheduling preferences.",
    ),
    "stage_exception_add": (
        StageExceptionAdd,
        "Stage a span when the user, or something they need, is unavailable: a trip, "
        'appointment, day off, or missing equipment ("no laptop today or tomorrow" → '
        'blocks_tags ["laptop"]). Use this, not a pin, for any availability statement; '
        "the solver then moves the affected blocks itself.",
    ),
    "stage_exception_remove": (
        StageExceptionRemove,
        "Stage removing an existing exception.",
    ),
    "stage_pin_add": (
        StagePinAdd,
        'Stage pinning a project to an exact span the user asked for ("put X on Thu '
        '6-8 PM"), or keeping a span free of one project. Never use it to work around '
        "unavailability; that is stage_exception_add.",
    ),
    "stage_pin_remove": (
        StagePinRemove,
        "Stage removing an existing pin or forbid.",
    ),
    "discard_staged": (
        DiscardStaged,
        "Drop a change that is staged but not confirmed yet.",
    ),
    "propose_schedule": (
        ProposeSchedule,
        "Solve a new schedule from everything staged and show it to the user for "
        "confirmation. Call it once, after staging, before your final reply.",
    ),
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model`'s JSON schema in strict-tool-use form: `$ref`s inlined, every object closed
    with all properties required, unsupported constraint keywords removed."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            extra = {k: v for k, v in node.items() if k != "$ref"}
            return walk({**target, **extra})
        out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED}
        if out.get("type") == "object":
            props = out.setdefault("properties", {})
            out["required"] = list(props)
            out["additionalProperties"] = False
        return out

    return walk(raw)


# Strict tool use compiles a grammar per request, and the API caps its size. Three tools carry
# the week-window shape and push the total over (measured 2026-11-17); they run non-strict, and
# the server's draft validation (`TurnStaging.call`) is the guard for them.
NON_STRICT = frozenset({"stage_project_create", "stage_project_update", "stage_preferences"})


def tool_definitions() -> list[dict[str, Any]]:
    """The static tool list sent on every agent request (tools are the first cached block)."""
    return [
        {
            "name": name,
            "description": description,
            "input_schema": strict_schema(model),
            "strict": name not in NON_STRICT,
        }
        for name, (draft, description) in TOOL_INPUTS.items()
        for model in [WIRE_MODELS.get(name, draft)]
    ]

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
        description="true only if the user said it must happen inside the preferred windows."
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
        "Stage a one-off unavailable span (trip, appointment, day off).",
    ),
    "stage_exception_remove": (
        StageExceptionRemove,
        "Stage removing an existing exception.",
    ),
    "stage_pin_add": (
        StagePinAdd,
        "Stage pinning a project to an exact span, or keeping a span free.",
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


def tool_definitions() -> list[dict[str, Any]]:
    """The static tool list sent on every agent request (tools are the first cached block)."""
    return [
        {
            "name": name,
            "description": description,
            "input_schema": strict_schema(model),
            "strict": True,
        }
        for name, (model, description) in TOOL_INPUTS.items()
    ]

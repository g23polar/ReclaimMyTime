"""Request bodies that have no counterpart in `app.db.models.staged` (ExceptionDraft,
BlockAction), and request-only subclasses of the staged project drafts/patch and preferences
that add the write-side size caps (issue #4). The staged models stay uncapped so rows stored
before the caps still parse."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.staged import (
    MAX_TAGS,
    MAX_WINDOWS_PER_DAY,
    DeadlineProjectDraft,
    PerennialProjectDraft,
    Preferences,
    ProjectPatch,
    Span,
    Tag,
    hhmm_to_minute,
)

# The staged models' validators (unique tags, window shape and order) still run on these.
CappedTags = Annotated[list[Tag], Field(max_length=MAX_TAGS)]
CappedWeekWindows = dict[str, Annotated[list[list[str]], Field(max_length=MAX_WINDOWS_PER_DAY)]]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExceptionDraft(_Body):
    """ARCH §5 ExceptionDraft: local dates/times, resolved with X-Timezone at staging (D10).
    The blocks_all / blocks_tags requirement is checked by the route (so the 422 can name
    `blocks_tags`)."""

    start_date: date
    start_time: str | None = None
    end_date: date
    end_time: str | None = None
    blocks_all: bool = False
    blocks_tags: CappedTags = []
    reason: Annotated[str, Field(max_length=500)] | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None:
            hhmm_to_minute(v)  # raises ValueError on a non-HH:MM value
        return v

    @field_validator("blocks_tags")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("tags must be unique")
        return v


class DeadlineProjectRequest(DeadlineProjectDraft):
    preferred_windows: CappedWeekWindows | None = None
    requirements: CappedTags = []


class PerennialProjectRequest(PerennialProjectDraft):
    preferred_windows: CappedWeekWindows | None = None
    requirements: CappedTags = []


ProjectDraftRequest = Annotated[
    DeadlineProjectRequest | PerennialProjectRequest, Field(discriminator="kind")
]


class ProjectPatchRequest(ProjectPatch):
    preferred_windows: CappedWeekWindows | None = None
    requirements: CappedTags | None = None


class PreferencesRequest(Preferences):
    schedulable_windows: CappedWeekWindows


class BlockComplete(_Body):
    action: Literal["complete"]


class BlockSkip(_Body):
    action: Literal["skip"]


class BlockEdit(_Body):
    """D7: absolute span (whole UTC minutes, start < end); the route checks it starts after
    now and lies inside the block's plan horizon."""

    action: Literal["edit"]
    span: Span


class BlockDelete(_Body):
    action: Literal["delete"]


BlockAction = Annotated[
    BlockComplete | BlockSkip | BlockEdit | BlockDelete, Field(discriminator="action")
]
BLOCK_ACTION_TAGS = frozenset({"complete", "skip", "edit", "delete"})

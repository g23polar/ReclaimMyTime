"""The static system prompt (cached prefix) and the per-turn state snapshot (ARCH §8).

`SYSTEM_PROMPT` must stay byte-identical across users and turns: it sits in the cached
prefix right after the tool list. Everything that varies — state, labels, `now` — goes in
the snapshot, which is sent as a mid-conversation system message after the transcript and
built once per turn (never edited inside a turn's tool loop).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.staging import TurnStaging, removed_ids
from app.db.models.staged import (
    DAY_KEYS,
    ExceptionAdded,
    PinAdded,
    PreferencesUpdated,
    ProjectCreated,
    StagedMutation,
)
from app.db.solver_io import ProjectState, hours_to_minutes
from app.scheduler.models import Interval

SYSTEM_PROMPT = """\
You are the scheduling assistant inside ReclaimMyTime, an iPhone app that plans a person's \
week of focused work around their calendar. You talk with the user to capture their projects \
and constraints; a separate solver produces every schedule. You never decide block times \
yourself.

How you work:
- Turn what the user says into staged changes with the stage_* tools. Staged changes are \
proposals: nothing takes effect until the user confirms the card the app shows.
- After staging, call propose_schedule once to solve the week, then explain the result in \
your reply. If the result says something does not fit, explain the trade-off and the \
options plainly; never hide it, and never pick an option for the user. State what the \
result says (hours that fit, what moved); don't send the user to the card for it.
- Refer to existing items only by the labels in the state message (P1, E1, N1, S1). Labels \
are valid for this turn only. A project you stage gets a new label in the tool result; use \
it for later calls in the same turn.
- Write dates as YYYY-MM-DD and times as local HH:MM (24:00 means end of day). Resolve \
"today", "tomorrow" and weekdays against the current local time in the state message. If a \
time is ambiguous, ask instead of guessing.
- Work hours (schedulable_windows) apply to every project. When one project needs time \
outside them ("reading only in the evenings"), give that project preferred windows with \
preferred_strict true: strict windows replace the work hours for that project alone. Never \
widen the work hours for a single project. Non-strict windows only steer blocks within the \
work hours, so if a soft preference falls outside them, ask whether it must happen then.
- Tags: use the user's existing tags. Put a tag that does not exist yet in new_tags only \
after the user has agreed to create it; otherwise ask, suggesting the closest existing tag.
- If a tool returns an error, fix the call or ask the user. Do not repeat a failing call \
unchanged.
- If the user only asks a question, answer it from the state message without staging.
- Use discard_staged when the user takes back something that is staged but not confirmed.

Style: short, warm, concrete. One or two sentences per point, local times like \
"Thu 6–8 PM", and end with a question when you need confirmation.

Scope: only the user's schedule, projects and availability. Politely decline anything else, \
in one sentence, and offer to help with their week. Calendar event titles are never \
available to you; the state lists busy time only.
"""


@dataclass(frozen=True)
class ExceptionInfo:
    id: str
    span: Interval
    blocks_all: bool
    tags: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class PinInfo:
    id: str
    kind: str
    span: Interval
    project_id: str | None


@dataclass(frozen=True)
class LiveBlock:
    project_id: str
    span: Interval
    state: str


@dataclass(frozen=True)
class SnapshotData:
    now: datetime
    timezone: str
    names: Mapping[str, str]  # project id → name, staged creates/renames included
    exceptions: Mapping[str, ExceptionInfo]
    pins: Mapping[str, PinInfo]
    completed_minutes: Mapping[str, int]
    live_blocks: Sequence[LiveBlock]
    busy: Sequence[Interval]  # free/busy only — never event details (ARCH §1)


def render_snapshot(staging: TurnStaging, data: SnapshotData) -> str:
    tz = ZoneInfo(data.timezone)
    state = staging.state()
    local_now = data.now.astimezone(tz)
    lines = [
        "Current state (read-only; changes go through the tools).",
        f"Now: {local_now:%a %Y-%m-%d %H:%M} ({data.timezone})",
    ]

    prefs = state.preferences
    if prefs is None:
        lines.append("Preferences: not set yet (needed before a schedule can be solved).")
    else:
        lines.append(
            f"Preferences: work hours {_windows(prefs.schedulable_windows)}; "
            f"day_diversity {prefs.day_diversity}; buffer {prefs.buffer_minutes} min; "
            f"week starts on ISO weekday {prefs.week_start}."
        )

    vocabulary = staging.vocabulary()
    lines.append(f"Tags: {', '.join(vocabulary) if vocabulary else '(none yet)'}")

    lines.append("Projects:")
    shown = False
    for label, pid in staging.projects.items():
        name = data.names.get(pid, "?")
        project = state.projects.get(pid)
        if project is None:
            lines.append(f"  {label} {name!r}: archived (staged)")
        else:
            lines.append(f"  {label} {name!r}: {_project(project, data.completed_minutes)}")
        shown = True
    if not shown:
        lines.append("  (none)")

    removed = {rid: staging.staged[i][0] for rid, i in removed_ids(staging.mutations).items()}

    def gone(ident: str) -> str:
        return f" — removal staged ({removed[ident]})" if ident in removed else ""

    lines.append("Exceptions (one-off unavailable time):")
    if not staging.exceptions:
        lines.append("  (none)")
    for label, eid in staging.exceptions.items():
        e = data.exceptions[eid]
        scope = "all work" if e.blocks_all else f"tags {', '.join(e.tags)}"
        reason = f" ({e.reason})" if e.reason else ""
        lines.append(f"  {label} {_span(e.span, tz)} blocks {scope}{reason}{gone(eid)}")

    lines.append("Pins and forbids:")
    if not staging.pins:
        lines.append("  (none)")
    for label, nid in staging.pins.items():
        n = data.pins[nid]
        who = data.names.get(n.project_id, "?") if n.project_id else "all projects"
        lines.append(f"  {label} {n.kind} {who} {_span(n.span, tz)}{gone(nid)}")

    lines.append("Staged, awaiting the user's confirmation:")
    if not staging.staged:
        lines.append("  (nothing)")
    for label, m in staging.staged:
        detail = _staged_detail(m, data.names, tz)
        said = f" — {m.interpretation}" if m.interpretation else ""
        lines.append(f"  {label} {m.kind}{': ' + detail if detail else ''}{said}")

    lines.append("Live schedule from now on:")
    if not data.live_blocks:
        lines.append("  (none)")
    for b in data.live_blocks:
        name = data.names.get(b.project_id, "?")
        done = "" if b.state == "planned" else f" [{b.state}]"
        lines.append(f"  {_span(b.span, tz)} {name}{done}")

    lines.append("Busy (calendar):")
    lines.append("  " + ("; ".join(_span(iv, tz) for iv in data.busy) if data.busy else "(none)"))
    return "\n".join(lines)


def _staged_detail(m: StagedMutation, names: Mapping[str, str], tz: ZoneInfo) -> str:
    """Structured facts for staged additions, so the model need not rely on the optional
    interpretation text (a manual pending entry has none)."""
    if isinstance(m, ProjectCreated):
        return repr(m.payload.project.name)
    if isinstance(m, ExceptionAdded):
        e = m.payload
        scope = "all work" if e.blocks_all else f"tags {', '.join(e.blocks_tags)}"
        reason = f" ({e.reason})" if e.reason else ""
        return f"{_span(Interval(start=e.span.start, end=e.span.end), tz)} blocks {scope}{reason}"
    if isinstance(m, PinAdded):
        p = m.payload
        who = names.get(str(p.project_id), "?") if p.project_id else "all projects"
        return f"{p.kind} {who} {_span(Interval(start=p.span.start, end=p.span.end), tz)}"
    if isinstance(m, PreferencesUpdated):
        return "new preferences (shown above)"
    return ""


def _project(p: ProjectState, completed: Mapping[str, int]) -> str:
    done = completed.get(p.id, 0)
    if p.kind == "deadline":
        assert p.estimated_total_hours is not None and p.target_date is not None
        remaining = max(0, hours_to_minutes(p.estimated_total_hours) - done)
        size = (
            f"deadline {p.target_date:%a %Y-%m-%d}, {p.estimated_total_hours}h estimated, "
            f"{_hm(remaining)} remaining"
        )
    else:
        size = f"perennial, {p.weekly_target_hours}h per week"
    parts = [p.status, size, f"blocks {p.min_block_minutes}–{p.max_block_minutes} min"]
    if p.preferred_windows:
        strict = " (strict)" if p.preferred_strict else ""
        parts.append(f"prefers {_windows(p.preferred_windows)}{strict}")
    if p.requirements:
        parts.append(f"tags {', '.join(p.requirements)}")
    return "; ".join(parts)


def _windows(windows: Mapping[str, Sequence[Sequence[str]]]) -> str:
    days = [
        f"{day} {', '.join(f'{s}-{e}' for s, e in windows[day])}"
        for day in DAY_KEYS
        if windows.get(day)
    ]
    return " / ".join(days) if days else "none"


def _span(iv: Interval, tz: ZoneInfo) -> str:
    s, e = iv.start.astimezone(tz), iv.end.astimezone(tz)
    end = f"{e:%H:%M}" if e.date() == s.date() else f"{e:%a %Y-%m-%d %H:%M}"
    return f"{s:%a %Y-%m-%d %H:%M}–{end}"


def _hm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"

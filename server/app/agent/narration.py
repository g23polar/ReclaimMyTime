"""Deterministic narration for proposals the agent did not narrate (T3.3, ARCH §8).

Manual edits and system proposals get `Changeset.narration` from here; the agent handler
uses it too when it proposes on the model's behalf (staged but no `propose_schedule`). Pure:
no LLM, no I/O. Inputs are the API views (`plan_view`, `diff_view`) plus a project-name map,
so the text matches exactly what the client renders.

Shape (contracts fixtures): "<what changed>: <effect on the week>. <shortfalls>. Options: …"
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.db.models.staged import (
    ExceptionAdded,
    ExceptionRemoved,
    PinAdded,
    PinRemoved,
    PreferencesUpdated,
    ProjectArchived,
    ProjectCreated,
    ProjectUpdated,
    StagedMutation,
)

_MOVES_LISTED = 3  # more changed blocks than this are summarized as counts


def narrate(
    *,
    staged: Sequence[StagedMutation],
    plan: Mapping[str, Any] | None,
    diff: Mapping[str, Any] | None,
    names: Mapping[str, str],
    timezone: str | None,
) -> str:
    """`timezone` renders spans in local time; None (no plan and no device timezone at hand)
    leaves spans out rather than showing them in the wrong zone."""
    tz = None if timezone is None else ZoneInfo(timezone)
    lead = _capitalize(
        _lead(staged, names, tz, first_week=diff is not None and diff["against_plan_id"] is None)
    )
    if plan is None or diff is None:
        return f"{lead}. Your schedule appears once your preferences are set."
    sentences = [f"{lead}: {_effect(diff, names, tz, echo=_pin_echo(staged))}"]
    infeasibility = plan.get("infeasibility")
    if infeasibility:
        sentences.extend(_problems(infeasibility, names, tz))
        options = [_option(r, names) for r in infeasibility["relaxations"]]
        if options:
            sentences.append("Options: " + _join(options, "or"))
    return " ".join(s if s.endswith(".") else s + "." for s in sentences)


def project_names(staged: Sequence[StagedMutation], committed: Mapping[str, str]) -> dict[str, str]:
    """Committed names overlaid with names from staged creates/renames."""
    names = dict(committed)
    for m in staged:
        if isinstance(m, ProjectCreated):
            names[str(m.payload.project_id)] = m.payload.project.name
        elif isinstance(m, ProjectUpdated) and m.payload.changes.name is not None:
            names[str(m.payload.project_id)] = m.payload.changes.name
    return names


# ---------------------------------------------------------------- lead clause


def _lead(
    staged: Sequence[StagedMutation],
    names: Mapping[str, str],
    tz: ZoneInfo | None,
    *,
    first_week: bool,
) -> str:
    if not staged:
        return "Schedule refreshed"
    m = staged[-1]  # the write that triggered this proposal
    more = len(staged) - 1
    suffix = f" (plus {more} other change{'s' if more > 1 else ''})" if more else ""
    return _describe(m, names, tz, first_week=first_week) + suffix


def _describe(
    m: StagedMutation, names: Mapping[str, str], tz: ZoneInfo | None, *, first_week: bool
) -> str:
    if isinstance(m, ProjectCreated):
        d = m.payload.project
        size = f"up to {_duration(d.max_block_minutes)} at a time"
        if d.kind == "perennial":
            return f"Add {d.name}: {_hours(d.weekly_target_hours)} every week, {size}"
        return f"Add {d.name}: {_hours(d.estimated_total_hours)} by {_date(d.target_date)}"
    if isinstance(m, ProjectUpdated):
        name = _name(names, m.payload.project_id)
        c = m.payload.changes
        if c.status == "paused":
            return f"{name} is paused"
        if c.status == "done":
            return f"{name} is done"
        if c.weekly_target_hours is not None:
            return f"{name} is now {_hours(c.weekly_target_hours)} a week"
        if c.target_date is not None:
            return f"{name} is now due {_date(c.target_date)}"
        if c.estimated_total_hours is not None:
            return f"{name} is now estimated at {_hours(c.estimated_total_hours)}"
        return f"{name} is updated"
    if isinstance(m, ProjectArchived):
        return f"{_name(names, m.payload.project_id)} is archived"
    if isinstance(m, PreferencesUpdated):
        return "Your first week" if first_week else "Your preferences are updated"
    if isinstance(m, ExceptionAdded):
        what = m.payload.reason or _span(m.payload.span.start, m.payload.span.end, tz)
        if m.payload.blocks_all:
            return f"{what} is blocked"
        return f"{what} is blocked for {_join(m.payload.blocks_tags, 'and')} work"
    if isinstance(m, ExceptionRemoved):
        return "An exception is removed"
    if isinstance(m, PinAdded):
        when = _span(m.payload.span.start, m.payload.span.end, tz)
        if m.payload.kind == "pin":
            return f"{_name(names, m.payload.project_id)} is pinned to {when}"
        if m.payload.project_id is None:
            return f"{when} is kept free"
        return f"{_name(names, m.payload.project_id)} won't be scheduled {when}"
    if isinstance(m, PinRemoved):
        return "A pin is removed"
    raise AssertionError(f"unhandled staged kind {m.kind}")


# ---------------------------------------------------------------- effect + problems


def _pin_echo(staged: Sequence[StagedMutation]) -> tuple[str, datetime, datetime] | None:
    """The block a triggering pin puts in place; the lead already states it."""
    m = staged[-1] if staged else None
    if not isinstance(m, PinAdded) or m.payload.kind != "pin" or m.payload.project_id is None:
        return None
    return str(m.payload.project_id), m.payload.span.start, m.payload.span.end


def _echoes(e: Mapping[str, Any], echo: tuple[str, datetime, datetime] | None) -> bool:
    if echo is None or e["change"] == "removed" or e["after"] is None:
        return False
    after = e["after"]
    return (e["project_id"], datetime.fromisoformat(after["start"]),
            datetime.fromisoformat(after["end"])) == echo  # fmt: skip


def _effect(
    diff: Mapping[str, Any],
    names: Mapping[str, str],
    tz: ZoneInfo | None,
    *,
    echo: tuple[str, datetime, datetime] | None = None,
) -> str:
    changed = [e for e in diff["entries"] if e["change"] != "unchanged"]
    if not changed:
        return "the schedule doesn't change"
    rest = [e for e in changed if not _echoes(e, echo)]
    if not rest:
        return "nothing else changes"
    changed = rest
    if diff["against_plan_id"] is None:
        projects = list(dict.fromkeys(_name(names, e["project_id"]) for e in changed))
        n = len(changed)
        return f"{n} block{'s' if n > 1 else ''} across {_join(projects, 'and')}"
    if len(changed) > _MOVES_LISTED:
        counts = {k: sum(e["change"] == k for e in changed) for k in ("moved", "added", "removed")}
        parts = [f"{n} block{'s' if n > 1 else ''} {verb}" for verb, n in (
            ("move", counts["moved"]), ("added", counts["added"]), ("removed", counts["removed"])
        ) if n]  # fmt: skip
        return _join(parts, "and")
    parts = []
    for e in changed:
        name = _name(names, e["project_id"])
        if e["change"] == "moved":
            parts.append(f"{name} moves to {_span_json(e['after'], tz)}")
        elif e["change"] == "added":
            parts.append(f"{name} is added {_span_json(e['after'], tz)}")
        else:
            parts.append(f"{name} on {_span_json(e['before'], tz)} is removed")
    return _join(parts, "and")


def _problems(
    infeasibility: Mapping[str, Any], names: Mapping[str, str], tz: ZoneInfo | None
) -> list[str]:
    out = []
    for v in infeasibility["violations"]:
        if v["kind"] == "demand_shortfall":
            name = _name(names, v["project_id"])
            out.append(
                f"{name} needs {_duration(v['required_minutes'])} this week, but only "
                f"{_duration(v['scheduled_minutes'])} fits"
            )
        else:
            what = "pin" if v["pin_kind"] == "pin" else "blocked time"
            whose = f" for {_name(names, v['project_id'])}" if v["project_id"] else ""
            out.append(
                f"Your {what}{whose} at {_span_json(v['span'], tz)} conflicts with "
                f"{_CONFLICTS[v['conflicts_with']]}"
            )
    return out


_CONFLICTS = {
    "busy": "your calendar",
    "exception": "an exception",
    "window": "your schedulable hours",
    "buffer": "the buffer between blocks",
    "overlap": "another block",
    "forbid": "a blocked time",
    "horizon": "the edge of this week",
    "before_now": "a time that has already passed",
    "preferred_window": "the project's required hours",
}


def _option(r: Mapping[str, Any], names: Mapping[str, str]) -> str:
    kind = r["kind"]
    name = _name(names, r.get("project_id"))
    if kind == "extend_deadline":
        due = date.fromisoformat(r["target_date"])
        return f"move {name}'s deadline to {_date(due)}"
    if kind == "reduce_estimate":
        return f"cut {name}'s estimate to {_hours(r['estimated_total_hours'])}"
    if kind == "reduce_weekly_target":
        return f"lower {name} to {_hours(r['weekly_target_hours'])} a week"
    if kind == "raise_max_block_minutes":
        return f"allow {name} up to {_duration(r['max_block_minutes'])} at a time"
    if kind == "pause_project":
        return f"pause {name}"
    if kind == "remove_pin":
        return "remove a pin"
    if kind == "remove_exception":
        return "drop an exception"
    raise AssertionError(f"unhandled relaxation {kind}")


# ---------------------------------------------------------------- formatting


def _name(names: Mapping[str, str], project_id: Any) -> str:
    return names.get(str(project_id), "A project")


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(items: Sequence[str], word: str) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {word} {items[-1]}"


def _hours(h: float | None) -> str:
    return f"{h:g}h"


def _duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m}m"
    return f"{h}h {m}m" if m else f"{h}h"


def _date(d: date | None) -> str:
    assert d is not None
    return f"{d:%a %b} {d.day}"


def _clock(t: datetime) -> tuple[str, str]:
    hour = t.hour % 12 or 12
    return (f"{hour}:{t.minute:02d}" if t.minute else str(hour)), ("AM" if t.hour < 12 else "PM")


def _span(start: datetime, end: datetime, tz: ZoneInfo | None) -> str:
    """Local "Thu 6–8 PM" / "Thu 11 AM–1 PM" / "Fri 2 PM–Sun 6 PM"."""
    if tz is None:
        return "that time"
    s, e = start.astimezone(tz), end.astimezone(tz)
    (st, sm), (et, em) = _clock(s), _clock(e)
    ends_at_midnight = (e.hour, e.minute) == (0, 0) and e - s < timedelta(days=1)
    if e.date() != s.date() and not ends_at_midnight:
        return f"{s:%a} {st} {sm}–{e:%a} {et} {em}"
    if e.date() != s.date():
        return f"{s:%a} {st} {sm}–midnight"
    if sm == em:
        return f"{s:%a} {st}–{et} {em}"
    return f"{s:%a} {st} {sm}–{et} {em}"


def _span_json(span: Mapping[str, str], tz: ZoneInfo | None) -> str:
    return _span(datetime.fromisoformat(span["start"]), datetime.fromisoformat(span["end"]), tz)

"""T3.3: deterministic narration (pure; no LLM)."""

import uuid

from app.agent.narration import narrate, project_names
from app.db.models.staged import parse_staged

TZ = "America/Los_Angeles"
PORT = "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01"
READ = "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02"
NAMES = {PORT: "Portfolio site", READ: "Reading"}
LIVE = "9c0d4e2b-6a1f-4c8e-a3b7-2f5d1e9c8a02"
NEW = "9c0d4e2b-6a1f-4c8e-a3b7-2f5d1e9c8a03"


def _staged(kind, payload, source="manual"):
    return parse_staged(
        [{"kind": kind, "source": source, "interpretation": None, "payload": payload}]
    )


def _span(start, end):
    return {"start": start, "end": end}


def _entry(project, change, before=None, after=None):
    return {
        "lineage_id": str(uuid.uuid4()),
        "project_id": project,
        "before": before,
        "after": after,
        "change": change,
    }


def _diff(*entries, against=LIVE):
    return {"plan_id": NEW, "against_plan_id": against, "entries": list(entries)}


def _plan(infeasibility=None):
    return {"timezone": TZ, "infeasibility": infeasibility}


PIN = _staged(
    "pin_added",
    {
        "pin_id": str(uuid.uuid4()),
        "project_id": PORT,
        "span": _span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z"),
        "kind": "pin",
        "created_from_block_id": None,
    },
)


def test_single_move_names_project_and_local_time():
    moved = _entry(
        PORT,
        "moved",
        before=_span("2026-11-19T03:00:00Z", "2026-11-19T05:00:00Z"),
        after=_span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z"),
    )
    text = narrate(staged=PIN, plan=_plan(), diff=_diff(moved), names=NAMES, timezone=TZ)
    assert text == "Portfolio site is pinned to Thu 6–8 PM: Portfolio site moves to Thu 6–8 PM."


def test_no_change():
    unchanged = _entry(PORT, "unchanged", before=_span("a", "b"), after=_span("a", "b"))
    text = narrate(staged=PIN, plan=_plan(), diff=_diff(unchanged), names=NAMES, timezone=TZ)
    assert text.endswith(": the schedule doesn't change.")


def test_many_changes_are_counted():
    s = _span("2026-11-18T17:00:00Z", "2026-11-18T18:00:00Z")
    entries = [_entry(READ, "moved", s, s)] * 3 + [_entry(READ, "removed", s, None)] * 2
    archive = _staged("project_archived", {"project_id": READ})
    text = narrate(staged=archive, plan=_plan(), diff=_diff(*entries), names=NAMES, timezone=TZ)
    assert text == "Reading is archived: 3 blocks move and 2 blocks removed."


def test_first_week_summary():
    s = _span("2026-11-18T17:00:00Z", "2026-11-18T18:00:00Z")
    prefs = _staged(
        "preferences_updated",
        {
            "preferences": {
                "timezone": TZ,
                "schedulable_windows": {"mon": [["08:00", "22:00"]]},
                "day_diversity": "mixed",
                "buffer_minutes": 15,
                "week_start": 1,
            }
        },
    )
    entries = [_entry(PORT, "added", None, s), _entry(READ, "added", None, s)]
    text = narrate(
        staged=prefs, plan=_plan(), diff=_diff(*entries, against=None), names=NAMES, timezone=TZ
    )
    assert text == "Your first week: 2 blocks across Portfolio site and Reading."


def test_infeasibility_lists_shortfall_conflicts_and_options():
    infeasibility = {
        "violations": [
            {
                "kind": "demand_shortfall",
                "project_id": PORT,
                "required_minutes": 1200,
                "scheduled_minutes": 405,
                "shortfall_minutes": 795,
            },
            {
                "kind": "pin_invalidated",
                "pin_id": str(uuid.uuid4()),
                "pin_kind": "pin",
                "project_id": READ,
                "span": _span("2026-11-22T23:00:00Z", "2026-11-23T00:00:00Z"),
                "conflicts_with": "busy",
                "exception_id": None,
                "related_lineage_ids": [],
            },
        ],
        "relaxations": [
            {"kind": "extend_deadline", "recovers_minutes": 795, "project_id": PORT,
             "target_date": "2026-11-27"},
            {"kind": "reduce_estimate", "recovers_minutes": 795, "project_id": PORT,
             "estimated_total_hours": 6.7},
            {"kind": "pause_project", "recovers_minutes": 120, "project_id": READ},
            {"kind": "remove_exception", "recovers_minutes": 60, "exception_id": str(uuid.uuid4())},
        ],
    }  # fmt: skip
    text = narrate(staged=PIN, plan=_plan(infeasibility), diff=_diff(), names=NAMES, timezone=TZ)
    assert "Portfolio site needs 20h this week, but only 6h 45m fits." in text
    assert "Your pin for Reading at Sun 3–4 PM conflicts with your calendar." in text
    assert text.endswith(
        "Options: move Portfolio site's deadline to Fri Nov 27, cut Portfolio site's estimate "
        "to 6.7h, pause Reading or drop an exception."
    )


def test_onboarding_without_plan_or_timezone_omits_spans():
    exc = _staged(
        "exception_added",
        {
            "exception_id": str(uuid.uuid4()),
            "span": _span("2026-11-20T17:00:00Z", "2026-11-20T19:00:00Z"),
            "blocks_all": True,
            "blocks_tags": [],
            "reason": None,
        },
    )
    text = narrate(staged=exc, plan=None, diff=None, names={}, timezone=None)
    assert text == "That time is blocked. Your schedule appears once your preferences are set."


def test_lead_counts_other_changes_and_new_project():
    created = _staged(
        "project_created",
        {
            "project_id": str(uuid.uuid4()),
            "project": {
                "kind": "perennial",
                "name": "Piano",
                "weekly_target_hours": 2,
                "max_block_minutes": 60,
            },
        },
    )
    text = narrate(staged=[*PIN, *created], plan=None, diff=None, names=NAMES, timezone=TZ)
    assert text.startswith("Add Piano: 2h every week, up to 1h at a time (plus 1 other change).")


def test_span_formats():
    from zoneinfo import ZoneInfo

    from app.agent.narration import _span_json

    tz = ZoneInfo(TZ)
    assert (
        _span_json(_span("2026-11-18T19:00:00Z", "2026-11-18T21:30:00Z"), tz) == "Wed 11 AM–1:30 PM"
    )
    assert (
        _span_json(_span("2026-11-19T06:00:00Z", "2026-11-19T08:00:00Z"), tz)
        == "Wed 10 PM–midnight"
    )
    assert (
        _span_json(_span("2026-11-20T22:00:00Z", "2026-11-23T02:00:00Z"), tz) == "Fri 2 PM–Sun 6 PM"
    )


def test_project_names_overlay_staged_creates_and_renames():
    pid = str(uuid.uuid4())
    staged = parse_staged(
        [
            {"kind": "project_created", "source": "agent", "interpretation": None,
             "payload": {"project_id": pid, "project": {"kind": "perennial", "name": "Piano",
                                                        "weekly_target_hours": 2,
                                                        "max_block_minutes": 60}}},
            {"kind": "project_updated", "source": "agent", "interpretation": None,
             "payload": {"project_id": READ, "changes": {"name": "Books"}}},
        ]
    )  # fmt: skip
    assert project_names(staged, NAMES) == {PORT: "Portfolio site", READ: "Books", pid: "Piano"}


def test_tag_scoped_exception_is_not_narrated_as_a_blackout():
    # Review crf-937b.
    exc = _staged(
        "exception_added",
        {
            "exception_id": str(uuid.uuid4()),
            "span": _span("2026-11-20T17:00:00Z", "2026-11-20T19:00:00Z"),
            "blocks_all": False,
            "blocks_tags": ["laptop", "deep-work"],
            "reason": None,
        },
    )
    text = narrate(staged=exc, plan=None, diff=None, names={}, timezone=TZ)
    assert text.startswith("Fri 9–11 AM is blocked for laptop and deep-work work.")

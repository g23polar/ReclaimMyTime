"""DB-free unit tests: local→absolute projection (DST), window validation, overlay,
SolverInput assembly, block-row mapping, lock key parity, and invariant (1) statically."""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.auth.repository import user_lock_statement as auth_lock
from app.db.models.staged import InvalidMutation, parse_staged, validate_week_windows
from app.db.solver_io import (
    ConstraintState,
    PreferencesNotSet,
    ProjectState,
    SourceBlock,
    block_rows,
    deadline_instant,
    hours_to_minutes,
    local_to_utc,
    overlay,
    project_windows,
    to_solver_input,
    week_horizon,
    week_start_for,
    zone,
)
from app.db.write_paths import user_lock_statement
from app.scheduler import FrozenBlock, Interval, PlannedBlock
from tests.db.conftest import GRANT, PORTFOLIO, deadline_project, prefs, staged, workout

NY = "America/New_York"
Z = UTC


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=Z)


# ---------------------------------------------------------------- DST projection


def test_fall_back_week_is_169_hours() -> None:
    # Week containing Sun 2026-11-01 (US DST ends 02:00 EDT → 01:00 EST).
    h = week_horizon(date(2026, 10, 26), NY)
    assert h.start == utc(2026, 10, 26, 4)  # 00:00 EDT
    assert h.end == utc(2026, 11, 2, 5)  # 00:00 EST
    assert h.duration_minutes == 169 * 60


def test_fall_back_windows_project_to_correct_instants() -> None:
    h = week_horizon(date(2026, 10, 26), NY)
    windows = {"sat": [["08:00", "20:00"]], "sun": [["08:00", "20:00"]]}
    assert project_windows(windows, h, NY) == [
        Interval(start=utc(2026, 10, 31, 12), end=utc(2026, 11, 1, 0)),  # EDT, -4
        Interval(start=utc(2026, 11, 1, 13), end=utc(2026, 11, 2, 1)),  # EST, -5
    ]


def test_ambiguous_hour_takes_earlier_instant() -> None:
    h = week_horizon(date(2026, 10, 26), NY)
    [w] = project_windows({"sun": [["01:00", "03:00"]]}, h, NY)
    assert w.start == utc(2026, 11, 1, 5)  # first 01:00 (EDT)
    assert w.end == utc(2026, 11, 1, 8)  # 03:00 EST
    assert w.duration_minutes == 180  # three real hours


def test_spring_forward_gap_moves_to_first_valid_instant() -> None:
    zi = zone(NY)
    assert local_to_utc(date(2027, 3, 14), 150, zi) == utc(2027, 3, 14, 7)  # 02:30 → 03:00 EDT
    h = week_horizon(date(2027, 3, 8), NY)
    assert h.duration_minutes == 167 * 60
    assert project_windows({"sun": [["02:10", "02:50"]]}, h, NY) == []  # wholly in the gap


def test_24_00_end_and_clipping_to_horizon() -> None:
    h = week_horizon(date(2026, 11, 16), "America/Los_Angeles")
    out = project_windows({"sun": [["22:00", "24:00"]]}, h, "America/Los_Angeles")
    assert out == [Interval(start=utc(2026, 11, 23, 6), end=utc(2026, 11, 23, 8))]
    assert out[-1].end == h.end


def test_deadline_instant_is_end_of_local_day_across_dst() -> None:
    assert deadline_instant(date(2026, 11, 1), NY) == utc(2026, 11, 2, 5)  # EST midnight
    assert deadline_instant(date(2026, 10, 31), NY) == utc(2026, 11, 1, 4)  # EDT midnight


def test_week_start_for_respects_preference() -> None:
    now = utc(2026, 11, 18, 3)  # Tue 2026-11-17 19:00 LA
    assert week_start_for(now, "America/Los_Angeles", 1) == date(2026, 11, 16)
    assert week_start_for(now, "America/Los_Angeles", 7) == date(2026, 11, 15)
    assert week_start_for(now, "Asia/Tokyo", 3) == date(2026, 11, 18)


def test_hours_to_minutes_is_exact() -> None:
    assert hours_to_minutes(Decimal("6.7")) == 402
    assert hours_to_minutes(2.5) == 150


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "windows",
    [
        {"xyz": []},
        {"mon": [["24:00", "24:00"]]},
        {"mon": [["09:00", "08:00"]]},
        {"mon": [["08:00", "10:00"], ["09:00", "11:00"]]},
        {"mon": [["8:00", "10:00"]]},
        {"mon": [["22:00", "02:00"]]},  # midnight crossing must be split
    ],
)
def test_invalid_windows_rejected(windows: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_week_windows(windows)


def test_staged_payload_validation() -> None:
    with pytest.raises(ValidationError):
        staged({"kind": "exception_added", "payload": {"exception_id": str(uuid.uuid4()),
               "span": {"start": "2026-11-16T10:00:00Z", "end": "2026-11-16T11:00:00Z"},
               "blocks_all": False, "blocks_tags": [], "reason": None}})  # fmt: skip
    off_grid = {"start": "2026-11-16T10:00:30Z", "end": "2026-11-16T11:00:00Z"}
    with pytest.raises(ValidationError):  # seconds are off the minute grid
        staged({"kind": "pin_added", "payload": {"pin_id": str(uuid.uuid4()),
               "project_id": None, "kind": "forbid", "created_from_block_id": None,
               "span": off_grid}})  # fmt: skip
    with pytest.raises(ValidationError):
        staged({"kind": "plan_confirmed", "payload": {}})
    with pytest.raises(ValidationError):
        staged(prefs(timezone="Mars/Olympus"))


def test_project_patch_round_trip_keeps_only_sent_fields() -> None:
    [m] = staged(
        {"kind": "project_updated",
         "payload": {"project_id": str(PORTFOLIO), "changes": {"preferred_windows": None}}}
    )  # fmt: skip
    dumped = m.model_dump(mode="json")
    assert dumped["payload"]["changes"] == {"preferred_windows": None}
    assert parse_staged([dumped]) == [m]


# ---------------------------------------------------------------- overlay + assembly


def _base_state() -> ConstraintState:
    return overlay(ConstraintState(preferences=None), staged(prefs(), workout()))


def test_overlay_and_solver_input() -> None:
    h = week_horizon(date(2026, 11, 16), "America/Los_Angeles")
    now = utc(2026, 11, 16, 14)
    st = overlay(
        _base_state(),
        staged(
            deadline_project(GRANT, hours=10.0, target="2026-11-20"),
            {"kind": "project_updated",
             "payload": {"project_id": str(GRANT), "changes": {"estimated_total_hours": 8.0}}},
        ),
    )  # fmt: skip
    inp = to_solver_input(
        st, horizon=h, now=now, tz="America/Los_Angeles", completed_minutes={str(GRANT): 60}
    )
    grant = next(p for p in inp.projects if p.id == str(GRANT))
    assert grant.remaining_minutes == 8 * 60 - 60
    assert grant.deadline == utc(2026, 11, 21, 8)
    assert grant.target_date == date(2026, 11, 20)
    w = next(p for p in inp.projects if p.id != str(GRANT))
    assert w.weekly_target_minutes == 180 and w.requirement_tags == ("gym",)
    assert len(inp.schedulable) == 7 and inp.busy == ()


def test_overlay_rejects_kind_mismatch_and_unknown_project() -> None:
    with pytest.raises(InvalidMutation):
        overlay(
            _base_state(),
            staged(
                {"kind": "project_updated",
                 "payload": {"project_id": str(next(iter(_base_state().projects))),
                             "changes": {"target_date": "2026-12-01"}}}
            ),
        )  # fmt: skip
    with pytest.raises(InvalidMutation):
        overlay(
            _base_state(),
            staged({"kind": "project_archived", "payload": {"project_id": str(GRANT)}})
            + staged({"kind": "project_updated",
                      "payload": {"project_id": str(GRANT), "changes": {"name": "x"}}}),
        )  # fmt: skip


def test_paused_projects_are_not_solved_and_no_prefs_raises() -> None:
    h = week_horizon(date(2026, 11, 16), "America/Los_Angeles")
    st = _base_state()
    pid = next(iter(st.projects))
    paused = ConstraintState(
        preferences=st.preferences,
        projects={pid: ProjectState(**{**st.projects[pid].__dict__, "status": "paused"})},
    )
    inp = to_solver_input(paused, horizon=h, now=h.start, tz="America/Los_Angeles")
    assert inp.projects == ()
    with pytest.raises(PreferencesNotSet):
        to_solver_input(ConstraintState(None), horizon=h, now=h.start, tz="UTC")


def test_block_rows_copy_frozen_verbatim_and_new_blocks_pending() -> None:
    h = week_horizon(date(2026, 11, 16), "America/Los_Angeles")
    lin_f, lin_n, pid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    f_span = Interval(start=h.start + timedelta(hours=16), end=h.start + timedelta(hours=17))
    st = _base_state()
    inp = to_solver_input(
        st,
        horizon=h,
        now=h.start + timedelta(hours=18),
        tz="America/Los_Angeles",
        frozen=[FrozenBlock(lineage_id=lin_f, project_id=pid, span=f_span, state="skipped")],
    )
    n_span = Interval(start=h.start + timedelta(hours=40), end=h.start + timedelta(hours=41))
    rows = block_rows(
        uuid.uuid4(),
        uuid.uuid4(),
        inp,
        [PlannedBlock(lineage_id=lin_n, project_id=pid, span=n_span)],
        {lin_f: SourceBlock("skipped", "synced", "evt-1", "etag-1")},
    )
    assert [
        (str(r["lineage_id"]), r["state"], r["gcal_status"], r["gcal_event_id"]) for r in rows
    ] == [
        (lin_f, "skipped", "synced", "evt-1"),
        (lin_n, "planned", "pending", None),
    ]


# ---------------------------------------------------------------- lock key + invariant (1)


def test_lock_key_matches_auth() -> None:
    uid = uuid.uuid4()
    dialect = postgresql.dialect()
    ours, theirs = user_lock_statement(uid), auth_lock(uid)
    assert str(ours.compile(dialect=dialect)) == str(theirs.compile(dialect=dialect))
    assert "hashtext" in str(ours)


def test_invariant_1_only_persist_plan_creates_blocks_static() -> None:
    """No code under app/ inserts into `blocks` or `week_plans` except persist_plan."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    creators: set[str] = set()
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
        ]:
            for node in ast.walk(fn):
                is_insert = (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", ""))
                    in ("insert", "pg_insert")
                    and node.args
                    and getattr(node.args[0], "id", "") in ("Block", "WeekPlan")
                )
                ctor = isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                    "Block",
                    "WeekPlan",
                )
                raw = (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and (
                        "insert into blocks" in node.value.lower()
                        or "insert into week_plans" in node.value.lower()
                    )
                )
                if is_insert or ctor or raw:
                    creators.add(f"{path.relative_to(app_dir)}:{fn.name}")
    assert creators == {"db/write_paths.py:persist_plan"}

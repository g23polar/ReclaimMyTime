"""Response mappers, no database: transient ORM rows in, contract-valid JSON out."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import Range

from app.api.mappers import (
    DiffBlock,
    changeset_out,
    diff_out,
    exception_out,
    infeasibility_out,
    minutes_to_hours,
    pin_out,
    plan_out,
    project_out,
    staged_out,
    ts,
)
from app.db.models import AvailabilityException, Block, BlockPin, Changeset, Project, WeekPlan
from tests.api.contract import assert_conforms

U = "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b0{}".format
T = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)


def rng(start_h: float, end_h: float) -> Range[datetime]:
    return Range(T + timedelta(hours=start_h), T + timedelta(hours=end_h), bounds="[)")


def test_ts_is_utc_z() -> None:
    pst = timezone(timedelta(hours=-8))
    assert ts(datetime(2026, 11, 17, 10, 30, tzinfo=pst)) == "2026-11-17T18:30:00Z"
    assert ts(datetime(2026, 11, 17, 18, 30, 1, 500, tzinfo=UTC)) == "2026-11-17T18:30:01.000500Z"


def _project(**over: Any) -> Project:
    base: dict[str, Any] = dict(
        id=uuid.UUID(U(1)),
        name="Portfolio site",
        kind="deadline",
        target_date=date(2026, 11, 30),
        estimated_total_hours=Decimal("12.5"),
        weekly_target_hours=None,
        max_block_minutes=120,
        min_block_minutes=25,
        preferred_windows=None,
        preferred_strict=False,
        requirements=["laptop"],
        status="active",
        created_at=T,
    )
    return Project(**(base | over))


def test_project_out_both_kinds() -> None:
    deadline = project_out(_project())
    assert (deadline["estimated_total_hours"], deadline["weekly_target_hours"]) == (12.5, None)
    assert deadline["target_date"] == "2026-11-30" and deadline["created_at"] == ts(T)
    perennial = project_out(
        _project(
            kind="perennial",
            target_date=None,
            estimated_total_hours=None,
            weekly_target_hours=Decimal("4.0"),
            preferred_windows={"mon": [["06:00", "09:00"]]},
        )
    )
    assert perennial["weekly_target_hours"] == 4.0 and perennial["target_date"] is None
    for body in (deadline, perennial):
        assert_conforms("getProject", 200, body)


def test_exception_and_pin_out() -> None:
    e = AvailabilityException(
        id=uuid.uuid4(),
        span=rng(0, 24),
        blocks_all=False,
        blocks_tags=["laptop"],
        reason=None,
        created_at=T,
    )
    assert_conforms("getException", 200, exception_out(e))
    p = BlockPin(
        id=uuid.uuid4(),
        project_id=None,
        span=rng(1, 2),
        kind="forbid",
        created_from_block_id=None,
        created_at=T,
    )
    body = pin_out(p)
    assert body["span"] == {"start": "2026-11-16T09:00:00Z", "end": "2026-11-16T10:00:00Z"}
    assert_conforms("listPins", 200, {"pins": [body]})


RAW_INFEASIBILITY: dict[str, Any] = {
    "violations": [
        {"kind": "demand_shortfall", "project_id": U(4), "required_minutes": 1200,
         "scheduled_minutes": 405, "shortfall_minutes": 795},
        {"kind": "pin_invalidated", "pin_id": U(5), "pin_kind": "pin", "project_id": U(1),
         "span": {"start": "2026-11-16T10:00:00+00:00", "end": "2026-11-16T11:00:00Z"},
         "conflicts_with": "overlap", "exception_id": None, "related_lineage_ids": [U(6)]},
    ],
    "relaxations": [
        {"kind": "extend_deadline", "description": "x", "recovers_minutes": 795,
         "project_id": U(4), "target_date": "2026-11-27", "affected_ids": [U(4)]},
        {"kind": "reduce_estimate", "description": "x", "recovers_minutes": 795,
         "project_id": U(4), "remaining_minutes": 405},
        {"kind": "reduce_weekly_target", "description": "x", "recovers_minutes": 60,
         "project_id": U(2), "weekly_target_minutes": 185},
        {"kind": "raise_max_block_minutes", "description": "x", "recovers_minutes": 30,
         "project_id": U(2), "max_block_minutes": 90},
        {"kind": "pause_project", "description": "x", "recovers_minutes": 360, "project_id": U(1)},
        {"kind": "remove_pin", "description": "x", "recovers_minutes": 60, "pin_id": U(5)},
        {"kind": "remove_exception", "description": "x", "recovers_minutes": 480,
         "exception_id": U(7)},
    ],
    "verify_violations": [{"kind": "pin_conflict", "message": "user data must not leak"}],
}  # fmt: skip


def test_infeasibility_maps_solver_units_to_patch_units() -> None:
    out = infeasibility_out(RAW_INFEASIBILITY, {U(4): 600})
    assert out is not None and "verify_violations" not in out
    relax = {r["kind"]: r for r in out["relaxations"]}
    assert relax["reduce_estimate"] == {
        "kind": "reduce_estimate", "recovers_minutes": 795, "project_id": U(4),
        "estimated_total_hours": 16.7,  # (405 + 600 completed) / 60 = 16.75 → floor 16.7
    }  # fmt: skip
    assert relax["reduce_weekly_target"]["weekly_target_hours"] == 3.0  # 185 min → 3.08 → 3.0
    assert relax["extend_deadline"] == {
        "kind": "extend_deadline", "recovers_minutes": 795, "project_id": U(4),
        "target_date": "2026-11-27",
    }  # fmt: skip
    assert out["violations"][1]["span"]["start"] == "2026-11-16T10:00:00Z"
    assert infeasibility_out(None, {}) is None
    assert infeasibility_out({"violations": [], "relaxations": []}, {}) is None


def test_minutes_to_hours_floors_and_clamps() -> None:
    assert minutes_to_hours(185, Decimal(168)) == 3.0
    assert minutes_to_hours(1, Decimal(168)) == 0.1
    assert minutes_to_hours(20000, Decimal(168)) == 168.0


def _block(lineage: int, start_h: float, end_h: float, plan: uuid.UUID) -> Block:
    return Block(
        id=uuid.uuid4(),
        plan_id=plan,
        user_id=uuid.uuid4(),
        project_id=uuid.UUID(U(2)),
        lineage_id=uuid.UUID(f"1a2b3c4d-5e6f-4a7b-8c9d-0000000000{lineage:02d}"),
        span=rng(start_h, end_h),
        state="planned",
        gcal_status="pending",
        gcal_event_id=None,
        gcal_etag="secret-etag",
    )


def _plan(pid: uuid.UUID, infeasibility: dict[str, Any] | None = None) -> WeekPlan:
    return WeekPlan(
        id=pid,
        horizon=rng(0, 168),
        week_start=date(2026, 11, 16),
        timezone="America/Los_Angeles",
        version=2,
        status="proposed",
        solver_input={"secret": True},
        infeasibility=infeasibility,
        created_at=T,
        superseded_at=None,
    )


def test_plan_out_sorts_blocks_and_omits_internal_columns() -> None:
    pid = uuid.uuid4()
    blocks = [_block(2, 30, 31, pid), _block(1, 2, 3, pid)]
    body = plan_out(_plan(pid, RAW_INFEASIBILITY), blocks, {})
    assert [b["lineage_id"][-2:] for b in body["blocks"]] == ["01", "02"]
    assert "solver_input" not in body and "gcal_etag" not in body["blocks"][0]
    assert_conforms("getCurrentPlan", 200, body)


def test_diff_out_kinds_and_order() -> None:
    old, new = uuid.uuid4(), uuid.uuid4()
    before = [DiffBlock.of(_block(n, s, e, old)) for n, s, e in ((1, 2, 3), (2, 5, 6), (3, 9, 10))]
    after = [DiffBlock.of(_block(n, s, e, new)) for n, s, e in ((1, 2, 3), (2, 7, 8), (4, 4, 5))]
    body = diff_out(str(new), str(old), after, before)
    assert [(e["lineage_id"][-2:], e["change"]) for e in body["entries"]] == [
        ("01", "unchanged"), ("04", "added"), ("02", "moved"), ("03", "removed"),
    ]  # fmt: skip
    assert_conforms("getPlanDiff", 200, body)
    first_week = diff_out(str(new), None, after, [])
    assert {e["change"] for e in first_week["entries"]} == {"added"}
    assert_conforms("getPlanDiff", 200, first_week)


def test_staged_out_drops_unset_min_block_minutes_only() -> None:
    draft = {"name": "R", "kind": "perennial", "weekly_target_hours": 2.0,
             "max_block_minutes": 60, "min_block_minutes": None,
             "preferred_windows": None}  # fmt: skip
    m = {"kind": "project_created", "source": "manual", "interpretation": None,
         "payload": {"project_id": U(3), "project": draft}}  # fmt: skip
    out = staged_out([m])[0]["payload"]["project"]
    assert "min_block_minutes" not in out and out["preferred_windows"] is None
    assert draft["min_block_minutes"] is None  # input not mutated
    payload = {"project_id": U(3), "project": draft | {"min_block_minutes": 30}}
    with_min = staged_out([m | {"payload": payload}])
    assert with_min[0]["payload"]["project"]["min_block_minutes"] == 30


def test_changeset_out_planless_onboarding() -> None:
    cs = Changeset(
        id=uuid.uuid4(),
        plan_id=None,
        staged=[
            {"kind": "project_created", "source": "manual", "interpretation": None,
             "payload": {"project_id": U(3), "project": {
                 "name": "Reading", "kind": "perennial", "weekly_target_hours": 2.0,
                 "max_block_minutes": 60, "min_block_minutes": None,
                 "preferred_windows": None, "preferred_strict": False, "requirements": []}}}
        ],
        status="pending",
        reason="user_edit",
        created_at=T,
        resolved_at=None,
    )  # fmt: skip
    body = changeset_out(cs, None, None)
    assert (body["plan_id"], body["plan"], body["diff"], body["narration"]) == (None,) * 4
    assert_conforms("createProject", 202, body)

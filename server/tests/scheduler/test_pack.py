from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.scheduler import (
    SOLVER_VERSION,
    DemandShortfall,
    FrozenBlock,
    Infeasibility,
    Interval,
    Pin,
    PinInvalidated,
    PlannedBlock,
    ProjectInput,
    Relaxation,
    SolverInput,
    SolverOutput,
    pack,
    verify,
)

from .conftest import PACK_FIXTURES_DIR, load_case, load_pack_input


def _t(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 16 + day, h, m, tzinfo=UTC)


def _span(day: int, h0: int, h1: int, m0: int = 0, m1: int = 0) -> Interval:
    return Interval(start=_t(day, h0, m0), end=_t(day, h1, m1))


def _by_project(out: SolverOutput, pid: str) -> list[PlannedBlock]:
    return [b for b in out.blocks if b.project_id == pid]


def _shortfalls(out: SolverOutput) -> dict[str, DemandShortfall]:
    assert out.infeasibility is not None
    return {v.project_id: v for v in out.infeasibility.violations if isinstance(v, DemandShortfall)}


def _pack_verified(inp: SolverInput, previous: list[PlannedBlock] | None = None) -> SolverOutput:
    out = pack(inp, previous)
    assert verify(out.blocks, inp) == []
    return out


@pytest.mark.parametrize("name", sorted(p.stem for p in PACK_FIXTURES_DIR.glob("*.json")))
def test_every_pack_fixture_verifies_clean(name: str) -> None:
    out = _pack_verified(load_pack_input(name))
    assert out.solver_version == SOLVER_VERSION == "1.0.0"


# ------------------------------------------------------------ (a) max block


def test_a_two_hour_project_never_gets_more_than_two_hours() -> None:
    inp = load_pack_input("max_block_2h")
    out = _pack_verified(inp)
    assert out.blocks
    assert max(b.span.duration_minutes for b in out.blocks) <= 120
    assert sum(b.span.duration_minutes for b in out.blocks) == 900
    assert out.infeasibility is None


def test_block_boundaries_snap_to_five_minutes() -> None:
    inp = load_pack_input("no_laptop_baseline")
    inp = inp.model_copy(
        update={"now": _t(0, 7, 3), "buffer_minutes": 7, "busy": (_span(0, 9, 10, 2, 11),)}
    )
    out = _pack_verified(inp)
    for b in out.blocks:
        assert b.span.start.minute % 5 == 0 and b.span.end.minute % 5 == 0


# --------------------------------------------------------- (b) no laptop


def test_b_no_laptop_exception_displaces_only_laptop_projects() -> None:
    inp = load_pack_input("no_laptop_tue_wed")
    out = _pack_verified(inp)
    exc = inp.exceptions[0].span
    hits = {b.project_id for b in out.blocks if b.span.overlaps(exc)}
    assert "portfolio" not in hits
    assert hits >= {"workout", "reading"}
    # Displaced laptop work moves elsewhere: demand still fully met.
    assert sum(b.span.duration_minutes for b in _by_project(out, "portfolio")) == 600
    # Non-laptop projects are not displaced from Tue–Wed.
    base = _pack_verified(load_pack_input("no_laptop_baseline"))
    for pid in ("workout", "reading"):
        assert sum(b.span.duration_minutes for b in _by_project(out, pid)) == sum(
            b.span.duration_minutes for b in _by_project(base, pid)
        )


# ---------------------------------------------------------- (c) diversity


def _projects_per_day(out: SolverOutput) -> float:
    days: dict[object, set[str]] = defaultdict(set)
    for b in out.blocks:
        days[b.span.start.date()].add(b.project_id)
    return sum(len(v) for v in days.values()) / len(days)


def test_c_focused_and_mixed_differ_in_day_composition() -> None:
    focused = _pack_verified(load_pack_input("diversity_focused"))
    mixed = _pack_verified(load_pack_input("diversity_mixed"))
    assert _projects_per_day(focused) == 1.0
    assert _projects_per_day(mixed) >= 2.5
    for out in (focused, mixed):
        for pid in ("alpha", "beta", "gamma"):
            assert sum(b.span.duration_minutes for b in _by_project(out, pid)) == 480


def test_mixed_avoids_back_to_back_and_spreads_deadline_sessions() -> None:
    out = _pack_verified(load_pack_input("diversity_mixed"))
    for pid in ("alpha", "beta", "gamma"):
        days = [b.span.start.date() for b in _by_project(out, pid)]
        assert len(days) == len(set(days))  # one sitting per day


def test_perennial_rhythm_keeps_the_same_time_of_day() -> None:
    out = _pack_verified(load_pack_input("no_laptop_baseline"))
    starts = {b.span.start.time() for b in _by_project(out, "reading")}
    assert len(starts) <= 2
    workout = _by_project(out, "workout")
    # Non-strict preferred window 07:00–10:00 (schedulable from 08:00) is honoured.
    assert sum(1 for b in workout if b.span.end.hour <= 10) >= 3


def test_strict_preferred_window_is_hard() -> None:
    inp = load_pack_input("no_laptop_baseline")
    projects = tuple(
        p.model_copy(update={"preferred_windows_strict": True}) if p.id == "workout" else p
        for p in inp.projects
    )
    inp = inp.model_copy(update={"projects": projects})
    out = _pack_verified(inp)
    assert all(b.span.end.hour <= 10 for b in _by_project(out, "workout"))


def test_days_keep_slack_unless_demand_requires() -> None:
    inp = load_pack_input("max_block_2h")
    out = _pack_verified(inp)
    per_day: dict[object, int] = defaultdict(int)
    for b in out.blocks:
        per_day[b.span.start.date()] += b.span.duration_minutes
    assert max(per_day.values()) * 100 <= 12 * 60 * 85
    # Demand that needs the slack gets it.
    tight = inp.model_copy(update={"schedulable": (_span(0, 8, 12),)})
    projects = (inp.projects[0].model_copy(update={"remaining_minutes": 225}),)
    out = _pack_verified(tight.model_copy(update={"projects": projects}))
    assert sum(b.span.duration_minutes for b in out.blocks) == 225


# --------------------------------------------------------- (d) infeasible


def test_d_infeasible_demand_returns_partial_plan_and_relaxations() -> None:
    inp = load_pack_input("infeasible_demand")
    out = _pack_verified(inp)
    assert out.blocks
    short = _shortfalls(out)
    thesis = short["thesis"]
    assert thesis.required_minutes == 3000
    assert thesis.scheduled_minutes == sum(
        b.span.duration_minutes for b in _by_project(out, "thesis")
    )
    assert thesis.shortfall_minutes == 3000 - thesis.scheduled_minutes > 0
    assert out.infeasibility is not None
    relax = out.infeasibility.relaxations
    assert relax and all(r.recovers_minutes > 0 for r in relax)
    kinds = {r.kind for r in relax}
    assert {"extend_deadline", "reduce_estimate", "reduce_weekly_target"} <= kinds
    ext = next(r for r in relax if r.kind == "extend_deadline")
    assert ext.target_date is not None and ext.target_date.isoformat() > "2026-11-22"
    # Priced by re-packing: a later deadline clears thesis's shortfall and, by lowering this
    # week's thesis load, frees room for some of gym's.
    gym = short["gym"].shortfall_minutes
    assert thesis.shortfall_minutes < ext.recovers_minutes <= thesis.shortfall_minutes + gym
    assert ext.affected_ids == ("gym", "thesis")
    _assert_relaxations_exact(inp, out)


def test_infeasible_without_any_free_time_never_raises() -> None:
    inp = load_pack_input("infeasible_demand").model_copy(update={"schedulable": ()})
    out = _pack_verified(inp)
    assert out.blocks == ()
    assert set(_shortfalls(out)) == {"thesis", "gym"}


def test_raise_max_block_when_max_length_binds() -> None:
    inp = load_pack_input("max_block_2h")
    one_window = inp.model_copy(update={"schedulable": (_span(0, 8, 14),)})
    projects = (inp.projects[0].model_copy(update={"min_block_minutes": 120}),)
    inp = one_window.model_copy(update={"projects": projects, "buffer_minutes": 60})
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    raise_ = [r for r in out.infeasibility.relaxations if r.kind == "raise_max_block_minutes"]
    assert raise_ and raise_[0].max_block_minutes and raise_[0].max_block_minutes > 120


def test_pause_and_remove_pin_relaxations_when_they_bind() -> None:
    inp = load_pack_input("max_block_2h")
    inp = inp.model_copy(
        update={
            "schedulable": (_span(0, 8, 12),),
            "pins": (Pin(id="pin-x", kind="pin", span=_span(0, 8, 10), project_id="other"),),
            "projects": (
                inp.projects[0].model_copy(update={"remaining_minutes": 240}),
                inp.projects[0].model_copy(update={"id": "other", "remaining_minutes": 0}),
            ),
        }
    )
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    kinds = {r.kind: r for r in out.infeasibility.relaxations}
    assert kinds["remove_pin"].pin_id == "pin-x"
    assert kinds["pause_project"].project_id == "other"
    assert all(r.recovers_minutes > 0 for r in kinds.values())


# --------------------------------------------------------------- (e) pins


def test_e_pin_on_busy_time_is_invalidated_and_rest_verifies() -> None:
    inp = load_pack_input("pin_vs_busy")
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    [inv] = [v for v in out.infeasibility.violations if isinstance(v, PinInvalidated)]
    assert inv.pin_id == "pin-busy"
    assert inv.conflicts_with == "busy"
    assert inv.span == inp.pins[0].span
    assert not any(b.span == inv.span for b in out.blocks)
    assert any(v.kind == "busy" for v in out.infeasibility.verify_violations)
    # The valid pin is placed exactly, pinned, keeping its originating lineage.
    [ok] = [b for b in out.blocks if b.pinned]
    assert ok.span == next(p.span for p in inp.pins if p.id == "pin-ok")
    assert ok.lineage_id == "L-ok"
    assert ok.span.duration_minutes == 180  # pins are exempt from max_block (O6)
    assert not any(b.lineage_id == "L-edited" for b in out.blocks)


def test_pin_conflict_order_reports_every_conflict_calendar_first() -> None:
    inp = load_pack_input("pin_vs_busy")
    exc = {
        "id": "exc-x",
        "span": _span(1, 9, 10).model_dump(mode="json"),
        "tags": ["laptop"],
        "blocks_all": True,
    }
    inp = SolverInput.model_validate({**inp.model_dump(mode="json"), "exceptions": [exc]})
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    inv = [v for v in out.infeasibility.violations if isinstance(v, PinInvalidated)]
    assert [(v.conflicts_with, v.exception_id) for v in inv] == [
        ("busy", None),
        ("exception", "exc-x"),
    ]


def test_overlapping_pins_first_wins_second_is_invalidated() -> None:
    inp = load_pack_input("pin_vs_busy")
    rival = Pin(id="pin-rival", kind="pin", span=_span(2, 16, 18), project_id="reading")
    out = _pack_verified(inp.model_copy(update={"pins": (*inp.pins, rival)}))
    assert out.infeasibility is not None
    inv = {v.pin_id: v for v in out.infeasibility.violations if isinstance(v, PinInvalidated)}
    assert inv["pin-rival"].conflicts_with == "overlap"
    assert inv["pin-rival"].related_lineage_ids == ("L-ok",)


def test_pin_realized_by_frozen_block_is_not_duplicated() -> None:
    inp = load_pack_input("pin_vs_busy")
    ok = next(p for p in inp.pins if p.id == "pin-ok")
    frozen = FrozenBlock(lineage_id="L-ok", project_id="portfolio", span=ok.span, state="planned")
    inp = inp.model_copy(update={"frozen": (frozen,), "now": _t(2, 15)})
    out = _pack_verified(inp)
    assert not any(b.lineage_id == "L-ok" for b in out.blocks)
    assert not any(b.pinned for b in out.blocks)


def test_pinned_flag_only_on_pin_realizations() -> None:
    inp = load_pack_input("pin_vs_busy")
    out = _pack_verified(inp)
    pin_spans = {(p.project_id, p.span) for p in inp.pins if p.kind == "pin"}
    for b in out.blocks:
        assert b.pinned == ((b.project_id, b.span) in pin_spans)


def test_pin_without_origin_gets_deterministic_uuid5_lineage() -> None:
    case = load_case("valid_plan")
    out = _pack_verified(case.input)
    [pinned] = [b for b in out.blocks if b.pinned]
    assert uuid.UUID(pinned.lineage_id).version == 5


# ------------------------------------------------------------- (f) forbid


def test_f_forbid_keeps_span_empty_for_that_project_only() -> None:
    inp = load_pack_input("forbid_span")
    out = _pack_verified(inp)
    forbid = inp.pins[0].span
    assert not any(b.span.overlaps(forbid) for b in _by_project(out, "portfolio"))
    assert any(b.span.overlaps(forbid) for b in _by_project(out, "reading"))


def test_blanket_forbid_keeps_span_empty_for_everyone() -> None:
    inp = load_pack_input("forbid_span")
    blanket = inp.pins[0].model_copy(update={"project_id": None})
    out = _pack_verified(inp.model_copy(update={"pins": (blanket,)}))
    assert not any(b.span.overlaps(blanket.span) for b in out.blocks)


# ---------------------------------------------------------- (g) lineage


def test_g_moved_busy_keeps_lineage_of_displaced_session() -> None:
    inp = load_pack_input("lineage_reuse").model_copy(update={"busy": (_span(5, 15, 16),)})
    first = _pack_verified(inp)
    [tue] = [b for b in _by_project(first, "portfolio") if b.span.start.date() == _t(1, 0).date()]
    # The busy interval moves onto Tuesday's portfolio session.
    moved = inp.model_copy(update={"busy": (_span(1, 7, 10, 30),)})
    second = _pack_verified(moved, list(first.blocks))
    [again] = [b for b in second.blocks if b.lineage_id == tue.lineage_id]
    assert again.project_id == "portfolio"
    assert again.span != tue.span
    assert not again.span.overlaps(moved.busy[0])
    # Every untouched session keeps its lineage and span.
    untouched = {b.lineage_id: b.span for b in first.blocks if not b.span.overlaps(_span(1, 0, 23))}
    kept = {b.lineage_id: b.span for b in second.blocks}
    for lid, span in untouched.items():
        assert kept.get(lid) == span


def test_repack_with_previous_is_stable() -> None:
    inp = load_pack_input("no_laptop_tue_wed")
    first = pack(inp)
    assert pack(inp, list(first.blocks)).model_dump_json() == first.model_dump_json()


def test_lineage_never_reused_twice_or_across_projects_or_frozen() -> None:
    inp = load_pack_input("lineage_reuse")
    prev = [
        PlannedBlock(lineage_id="same", project_id="portfolio", span=_span(0, 8, 10)),
        PlannedBlock(lineage_id="same2", project_id="portfolio", span=_span(0, 8, 10)),
        PlannedBlock(lineage_id="wrong-project", project_id="nope", span=_span(1, 8, 10)),
        PlannedBlock(lineage_id="F-past", project_id="portfolio", span=_span(2, 8, 10)),
        PlannedBlock(lineage_id="far", project_id="portfolio", span=_span(6, 18, 20)),
    ]
    frozen = FrozenBlock(
        lineage_id="F-past", project_id="reading", span=_span(0, 1, 2), state="completed"
    )
    inp = inp.model_copy(update={"frozen": (frozen,)})
    out = _pack_verified(inp, prev)
    ids = [b.lineage_id for b in out.blocks]
    assert len(ids) == len(set(ids))
    assert "wrong-project" not in ids and "F-past" not in ids
    assert {"same", "same2"} <= set(ids)


def test_minted_lineages_are_deterministic_uuid5() -> None:
    inp = load_pack_input("lineage_reuse")
    a, b = pack(inp), pack(inp)
    assert [x.lineage_id for x in a.blocks] == [x.lineage_id for x in b.blocks]
    assert all(uuid.UUID(x.lineage_id).version == 5 for x in a.blocks)


# --------------------------------------------------------- determinism


@pytest.mark.parametrize("name", sorted(p.stem for p in PACK_FIXTURES_DIR.glob("*.json")))
def test_byte_identical_and_shuffle_invariant(name: str) -> None:
    inp = load_pack_input(name)
    first = pack(inp).model_dump_json()
    assert pack(inp).model_dump_json() == first
    shuffled = inp.model_copy(
        update={
            k: tuple(reversed(getattr(inp, k))) for k in ("projects", "pins", "busy", "exceptions")
        }
    )
    assert pack(shuffled).model_dump_json() == first
    raw = json.loads((PACK_FIXTURES_DIR / f"{name}.json").read_text())
    assert SolverInput.model_validate(raw["input"]) == inp


# --------------------------------------------------------------- models


def test_relaxation_kind_fields_are_enforced() -> None:
    Relaxation(kind="pause_project", description="", recovers_minutes=5, project_id="P")
    with pytest.raises(ValidationError):
        Relaxation(kind="bogus", description="", recovers_minutes=5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Relaxation(kind="remove_pin", description="", recovers_minutes=5)
    with pytest.raises(ValidationError):
        Relaxation(kind="pause_project", description="", recovers_minutes=0, project_id="P")
    with pytest.raises(ValidationError):
        Relaxation(
            kind="pause_project", description="", recovers_minutes=5, project_id="P", pin_id="x"
        )


def test_infeasibility_violations_discriminate_on_kind() -> None:
    raw = {
        "violations": [
            {
                "kind": "demand_shortfall",
                "project_id": "P",
                "required_minutes": 10,
                "scheduled_minutes": 4,
                "shortfall_minutes": 6,
            },
            {
                "kind": "pin_invalidated",
                "pin_id": "x",
                "pin_kind": "pin",
                "project_id": "P",
                "span": _span(0, 1, 2).model_dump(mode="json"),
                "conflicts_with": "busy",
            },
        ]
    }
    inf = Infeasibility.model_validate(raw)
    assert isinstance(inf.violations[0], DemandShortfall)
    assert isinstance(inf.violations[1], PinInvalidated)
    with pytest.raises(ValidationError):
        DemandShortfall(
            project_id="P", required_minutes=10, scheduled_minutes=4, shortfall_minutes=5
        )


def test_output_is_identical_across_hash_seeds() -> None:
    import subprocess
    import sys

    script = (
        "from tests.scheduler.conftest import load_pack_input\n"
        "from app.scheduler import pack\n"
        "for n in ['no_laptop_tue_wed', 'infeasible_demand', 'pin_vs_busy', 'forbid_span']:\n"
        "    print(pack(load_pack_input(n)).model_dump_json())\n"
    )
    server = Path(__file__).resolve().parents[2]
    outs = [
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=server,
            env={"PYTHONHASHSEED": seed, "PATH": "", "PYTHONPATH": str(server)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "12345")
    ]
    assert outs[0] and outs[0] == outs[1]


# ------------------------------------------- review fixes (2026-11-14 decisions)


def _project(pid: str, **kw: object) -> ProjectInput:
    fields: dict[str, object] = {
        "remaining_minutes": 0,
        "min_block_minutes": 60,
        "max_block_minutes": 120,
    }
    fields.update(kw)
    return ProjectInput(id=pid, **fields)  # type: ignore[arg-type]


def _week(**kw: object) -> SolverInput:
    fields: dict[str, object] = {
        "solver_version": "test",
        "now": _t(0, 0),
        "horizon": Interval(start=_t(0, 0), end=_t(7, 0)),
        "day_diversity": "mixed",
        "buffer_minutes": 0,
    }
    fields.update(kw)
    return SolverInput(**fields)  # type: ignore[arg-type]


def _total_shortfall(out: SolverOutput) -> int:
    return sum(v.shortfall_minutes for v in _shortfalls(out).values()) if out.infeasibility else 0


def _apply(inp: SolverInput, r: Relaxation) -> SolverInput:
    """The input the user would get by picking relaxation `r`."""
    if r.kind == "remove_exception":
        return inp.model_copy(
            update={"exceptions": tuple(e for e in inp.exceptions if e.id != r.exception_id)}
        )
    if r.kind == "remove_pin":
        return inp.model_copy(update={"pins": tuple(p for p in inp.pins if p.id != r.pin_id)})
    if r.kind == "pause_project":
        return inp.model_copy(
            update={
                "projects": tuple(p for p in inp.projects if p.id != r.project_id),
                "pins": tuple(p for p in inp.pins if p.project_id != r.project_id),
            }
        )
    update: dict[str, object] = {
        "reduce_estimate": {"remaining_minutes": r.remaining_minutes},
        "reduce_weekly_target": {"weekly_target_minutes": r.weekly_target_minutes},
        "raise_max_block_minutes": {"max_block_minutes": r.max_block_minutes},
    }.get(r.kind, {})
    if r.kind == "extend_deadline":
        assert r.target_date is not None
        old = next(p for p in inp.projects if p.id == r.project_id)
        assert old.target_date is not None and old.deadline is not None
        update = {
            "target_date": r.target_date,
            "deadline": old.deadline + (r.target_date - old.target_date),
        }
    return inp.model_copy(
        update={
            "projects": tuple(
                p.model_copy(update=update) if p.id == r.project_id else p for p in inp.projects
            )
        }
    )


def _assert_relaxations_exact(inp: SolverInput, out: SolverOutput) -> None:
    assert out.infeasibility is not None
    before = _total_shortfall(out)
    for r in out.infeasibility.relaxations:
        after = _pack_verified(_apply(inp, r))
        if r.kind == "pause_project":
            after_total = sum(
                v.shortfall_minutes
                for pid, v in (_shortfalls(after).items() if after.infeasibility else [])
            )
            paused = _shortfalls(out).get(r.project_id or "")
            before_total = before - (paused.shortfall_minutes if paused else 0)
            assert before_total - after_total == r.recovers_minutes, r
        else:
            assert before - _total_shortfall(after) == r.recovers_minutes, r


# 1. Deadlines bound placement.


def test_deadline_bounds_placement_and_reports_shortfall() -> None:
    wed_end = _t(3, 0)  # end of Wednesday 2026-11-18
    inp = _week(
        schedulable=(_span(3, 8, 12),),  # Thursday only
        projects=(
            _project(
                "essay",
                remaining_minutes=120,
                target_date=date(2026, 11, 18),
                deadline=wed_end,
            ),
        ),
    )
    out = _pack_verified(inp)
    assert out.blocks == ()
    short = _shortfalls(out)["essay"]
    assert (short.required_minutes, short.scheduled_minutes) == (120, 0)
    assert out.infeasibility is not None
    ext = [r for r in out.infeasibility.relaxations if r.kind == "extend_deadline"]
    assert ext and ext[0].target_date is not None and ext[0].target_date > date(2026, 11, 18)
    _assert_relaxations_exact(inp, out)


def test_pin_after_deadline_is_kept_but_does_not_satisfy_deadline_demand() -> None:
    # Review finding (T1.2 round 2): a Thursday pin must not count as on-time work
    # for a Wednesday deadline. The pin stays (user override, O6); the demand is short.
    wed_end = _t(3, 0)
    thu_pin = Pin(id="pin-thu", kind="pin", span=_span(3, 9, 10), project_id="essay")
    inp = _week(
        schedulable=(_span(3, 8, 12),),  # Thursday only
        pins=(thu_pin,),
        projects=(
            _project(
                "essay",
                remaining_minutes=60,
                target_date=date(2026, 11, 18),
                deadline=wed_end,
            ),
        ),
    )
    out = _pack_verified(inp)
    assert [b.span for b in out.blocks if b.pinned] == [thu_pin.span]
    short = _shortfalls(out)["essay"]
    assert (short.required_minutes, short.scheduled_minutes, short.shortfall_minutes) == (60, 0, 60)


def test_deadline_inside_horizon_never_has_blocks_after_it() -> None:
    inp = load_pack_input("max_block_2h")
    projects = tuple(p.model_copy(update={"deadline": _t(2, 12)}) for p in inp.projects)
    out = _pack_verified(inp.model_copy(update={"projects": projects}))
    assert out.blocks and all(b.span.end <= _t(2, 12) for b in out.blocks)


def test_periods_left_follow_the_deadline_instant() -> None:
    # Deadline two horizons out: half of the remaining work is due this week.
    inp = _week(
        schedulable=tuple(_span(d, 8, 12) for d in range(7)),
        projects=(
            _project(
                "book",
                remaining_minutes=600,
                target_date=date(2026, 11, 29),
                deadline=_t(14, 0),
            ),
        ),
    )
    out = _pack_verified(inp)
    assert sum(b.span.duration_minutes for b in out.blocks) == 300


# 2. Minted lineages never collide.


def test_minted_lineage_never_collides_with_unmatched_previous_monday_to_friday() -> None:
    """Monday's session (minted P:0) is displaced; Friday's new session must not inherit
    Monday's id by minting the same uuid5, or the diff shows a phantom move."""
    ns = uuid.UUID("6f1d3c2a-4b5e-5f60-8a7b-9c0d1e2f3a4b")
    monday = [
        PlannedBlock(
            lineage_id=str(uuid.uuid5(ns, name)), project_id="P", span=_span(0, 9 + i, 10 + i)
        )
        for i, name in enumerate(("P:0", "P:0:0", "P:1:0"))
    ]
    frozen = FrozenBlock(
        lineage_id=str(uuid.uuid5(ns, "P:0:1")),
        project_id="Q",
        span=_span(0, 1, 2),
        state="completed",
    )
    inp = _week(
        now=_t(0, 8),
        busy=(_span(0, 8, 23),),  # Monday is gone
        schedulable=(_span(0, 8, 23), _span(4, 8, 12)),  # Friday only, > 24h away
        frozen=(frozen,),
        projects=(_project("P", weekly_target_minutes=120, max_block_minutes=60), _project("Q")),
    )
    out = _pack_verified(inp, monday)
    ids = [b.lineage_id for b in out.blocks]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert not set(ids) & ({b.lineage_id for b in monday} | {frozen.lineage_id})


# 3. Credit frozen in-progress work.


def test_frozen_in_progress_minutes_count_toward_deadline_demand() -> None:
    inp = _week(
        now=_t(0, 8, 30),
        schedulable=(_span(0, 8, 18),),
        frozen=(
            FrozenBlock(lineage_id="F", project_id="D", span=_span(0, 8, 9), state="planned"),
            FrozenBlock(lineage_id="C", project_id="D", span=_span(0, 6, 7), state="completed"),
        ),
        projects=(_project("D", remaining_minutes=120, max_block_minutes=60),),
    )
    out = _pack_verified(inp)
    assert sum(b.span.duration_minutes for b in out.blocks) == 60
    assert out.infeasibility is None


# 4. `now` rounds up.


def test_now_rounds_up_so_no_block_starts_before_the_real_instant() -> None:
    now = datetime(2026, 11, 16, 8, 0, 30, tzinfo=UTC)
    inp = _week(
        now=now,
        schedulable=(_span(0, 8, 9),),
        projects=(_project("P", remaining_minutes=60, min_block_minutes=30, max_block_minutes=60),),
    )
    out = _pack_verified(inp)
    assert out.blocks
    assert all(b.span.start >= _t(0, 8, 5) for b in out.blocks)


# 5. Bounded relaxation work.


def test_relaxations_with_100_pins_finish_fast_and_stay_one_per_kind() -> None:
    pins = tuple(
        Pin(
            id=f"pin{i:03d}",
            kind="pin",
            span=_span(i % 7, 8 + i // 7, 9 + i // 7) if i // 7 < 14 else _span(i % 7, 22, 23),
            project_id="other",
        )
        for i in range(100)
    )
    inp = _week(
        schedulable=tuple(_span(d, 8, 23) for d in range(7)),
        pins=pins,
        projects=(
            _project("other", min_block_minutes=60, max_block_minutes=60),
            _project("thesis", remaining_minutes=6000, max_block_minutes=120),
        ),
    )
    t0 = time.perf_counter()
    out = pack(inp)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, elapsed
    assert verify(out.blocks, inp) == []
    assert out.infeasibility is not None
    kinds = [r.kind for r in out.infeasibility.relaxations]
    assert len(kinds) == len(set(kinds))  # one short project: one relaxation per kind
    assert "remove_pin" in kinds


# 6. No overflow; never lose the plan.


def test_huge_estimate_returns_partial_plan_without_extend_deadline() -> None:
    """5 min/week against 99,999.9 h: a naive extension lands past date.max."""
    inp = _week(
        schedulable=(Interval(start=_t(0, 8), end=_t(0, 8, 5)),),
        projects=(
            _project(
                "big",
                remaining_minutes=5_999_994,
                min_block_minutes=5,
                max_block_minutes=5,
                target_date=date(2026, 11, 22),
                deadline=_t(7, 0),
            ),
        ),
    )
    out = _pack_verified(inp)
    assert [b.span.duration_minutes for b in out.blocks] == [5]
    short = _shortfalls(out)["big"]
    assert (short.required_minutes, short.shortfall_minutes) == (5_999_994, 5_999_989)
    assert out.infeasibility is not None
    assert "extend_deadline" not in {r.kind for r in out.infeasibility.relaxations}


def test_extend_deadline_is_capped_two_years_out() -> None:
    inp = _week(
        schedulable=(_span(0, 8, 9),),
        projects=(
            _project(
                "slow",
                remaining_minutes=60 * 200,  # 200 weeks at 1 h/week
                target_date=date(2026, 11, 22),
                deadline=_t(7, 0),
            ),
        ),
    )
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    assert "extend_deadline" not in {r.kind for r in out.infeasibility.relaxations}
    ok = inp.model_copy(
        update={"projects": (inp.projects[0].model_copy(update={"remaining_minutes": 60 * 100}),)}
    )
    out = _pack_verified(ok)
    assert out.infeasibility is not None
    [ext] = [r for r in out.infeasibility.relaxations if r.kind == "extend_deadline"]
    assert ext.target_date is not None
    assert ext.target_date <= date(2026, 11, 16) + timedelta(days=730)


# 7. Exact recovery for every relaxation.


def test_demand_relaxation_recovery_is_measured_by_repacking() -> None:
    """A (perennial, 360/week) and B (deadline, 480) share 10 h; cutting B's estimate to
    what was scheduled lets A take back the slack, so B's shortfall only falls by 180."""
    inp = _week(
        schedulable=(_span(0, 8, 13), _span(1, 8, 13)),
        projects=(
            _project("A", weekly_target_minutes=360),
            _project("B", remaining_minutes=480, min_block_minutes=120, max_block_minutes=240),
        ),
    )
    out = _pack_verified(inp)
    assert out.infeasibility is not None
    [cut] = [r for r in out.infeasibility.relaxations if r.kind == "reduce_estimate"]
    assert cut.recovers_minutes == 180
    _assert_relaxations_exact(inp, out)


@pytest.mark.parametrize(
    "name", ["infeasible_demand", "max_block_2h", "pin_vs_busy", "no_laptop_tue_wed"]
)
def test_every_relaxation_recovers_exactly_what_it_claims(name: str) -> None:
    inp = load_pack_input(name)
    out = pack(inp)
    if out.infeasibility is not None and out.infeasibility.relaxations:
        _assert_relaxations_exact(inp, out)

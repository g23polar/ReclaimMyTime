"""Determinism and verify-as-oracle properties (ARCHITECTURE.md §4, T1.3).

- Stress strategies on top of `strategies.solver_inputs`: many (conflicting) pins, tagged
  exceptions, zero free time, horizons shorter than demand, perennial + deadline mixes,
  `focused` and `mixed`.
- Properties checked on every example (`_check`): the plan verifies clean; pin reports are
  well-formed; shortfalls match the plan; each relaxation, applied and re-packed, recovers
  what it claims.
- Two-`PYTHONHASHSEED` subprocess test over golden corpus inputs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.scheduler import (
    DemandShortfall,
    ExceptionInput,
    Pin,
    PinInvalidated,
    ProjectInput,
    Relaxation,
    SolverInput,
    SolverOutput,
    pack,
    verify,
)

from .golden.corpus_io import expected_path, input_path
from .strategies import HORIZON_MINUTES, TAGS, iv, solver_inputs

SERVER_DIR = Path(__file__).resolve().parents[2]

# ------------------------------------------------------------------ strategies


def _minutes(dt_offset: timedelta) -> int:
    return dt_offset // timedelta(minutes=1)


@st.composite
def _conflicting_pins(draw: st.DrawFn, inp: SolverInput) -> tuple[Pin, ...]:
    """6-20 pins: random, twins, overlapping each other, on busy, in exceptions, in forbids."""
    pids = [p.id for p in inp.projects]
    origin = inp.horizon.start
    anchors = [
        *(i for i in inp.busy),
        *(e.span for e in inp.exceptions),
        *(p.span for p in inp.pins),
    ]
    pins: list[Pin] = list(inp.pins)
    for i in range(draw(st.integers(6, 20))):
        mode = draw(st.sampled_from(["random", "twin", "overlap", "anchor", "forbid"]))
        if mode in ("twin", "overlap") and pins:
            base = draw(st.sampled_from(pins)).span
            shift = 0 if mode == "twin" else draw(st.integers(-90, 90))
            s = _minutes(base.start - origin) + shift
            e = _minutes(base.end - origin) + shift
        elif mode == "anchor" and anchors:
            a = draw(st.sampled_from(anchors))
            s = _minutes(a.start - origin) + draw(st.integers(-30, 30))
            e = s + draw(st.integers(15, 180))
        else:
            s = draw(st.integers(-120, HORIZON_MINUTES))
            e = s + draw(st.integers(15, 240))
        if e <= s:
            e = s + 15
        span = iv(s, e)
        if mode == "forbid":
            pins.append(
                Pin(
                    id=f"sf{i}",
                    kind="forbid",
                    span=span,
                    project_id=draw(st.none() | st.sampled_from(pids)),
                )
            )
        else:
            pins.append(
                Pin(
                    id=f"sp{i}",
                    kind="pin",
                    span=span,
                    project_id=draw(st.sampled_from(pids)),
                    created_from_lineage_id=draw(st.none() | st.sampled_from(["L-old", "L-x"])),
                )
            )
    return tuple(pins)


@st.composite
def _tagged_exceptions(draw: st.DrawFn) -> tuple[ExceptionInput, ...]:
    out = []
    for i in range(draw(st.integers(1, 5))):
        s = draw(st.integers(-600, HORIZON_MINUTES))
        out.append(
            ExceptionInput(
                id=f"sx{i}",
                span=iv(s, s + draw(st.integers(60, 3 * 1440))),
                tags=tuple(draw(st.lists(st.sampled_from(TAGS), min_size=1, max_size=3))),
                blocks_all=draw(st.integers(0, 4)) == 0,
            )
        )
    return tuple(out)


@st.composite
def _mixed_projects(draw: st.DrawFn, inp: SolverInput) -> tuple[ProjectInput, ...]:
    """Force at least one perennial and one deadline project (deadline inside or past)."""
    projects = list(inp.projects)
    for i, kind in enumerate(("perennial", "deadline")):
        lo = draw(st.integers(15, 90))
        hi = draw(st.integers(lo, 180))
        deadline = inp.horizon.start + timedelta(minutes=draw(st.integers(60, 3 * 7 * 1440)))
        projects.append(
            ProjectInput(
                id=f"mix{i}",
                remaining_minutes=draw(st.integers(0, 4000)),
                weekly_target_minutes=draw(st.integers(1, 900)) if kind == "perennial" else None,
                max_block_minutes=hi,
                min_block_minutes=lo,
                requirement_tags=tuple(draw(st.lists(st.sampled_from(TAGS), max_size=2))),
                target_date=None if kind == "perennial" else deadline.date(),
                deadline=None if kind == "perennial" else deadline,
            )
        )
    return tuple(projects)


def _flag(rate: int) -> st.SearchStrategy[bool]:
    """True roughly 1 in `rate`; False first, so shrinking heads to the plain case."""
    return st.sampled_from([False] * (rate - 1) + [True])


@st.composite
def stress_inputs(draw: st.DrawFn) -> SolverInput:
    inp = draw(solver_inputs())
    update: dict[str, object] = {"day_diversity": draw(st.sampled_from(["mixed", "focused"]))}
    if draw(_flag(2)):
        update["projects"] = draw(_mixed_projects(inp))
    if draw(_flag(2)):
        update["exceptions"] = (*inp.exceptions, *draw(_tagged_exceptions()))
    inp = inp.model_copy(update=update)
    if draw(_flag(2)):
        inp = inp.model_copy(update={"pins": draw(_conflicting_pins(inp))})
    if draw(_flag(8)):  # zero free time: every window busy
        inp = inp.model_copy(update={"busy": (*inp.busy, *inp.schedulable)})
    elif draw(_flag(8)):  # zero free time: no windows at all
        inp = inp.model_copy(update={"schedulable": ()})
    if draw(_flag(5)):
        # Horizon shorter than demand: half a day to two days from now, with demand to spare.
        start = max(0, _minutes(inp.now - inp.horizon.start))
        end = start + draw(st.integers(12 * 60, 2 * 1440))
        projects = tuple(
            p.model_copy(update={"remaining_minutes": max(p.remaining_minutes, end - start)})
            for p in inp.projects
        )
        inp = inp.model_copy(update={"horizon": iv(0, end), "projects": projects})
    # Re-validate: model_copy skips validators, and every case must be contract-valid.
    return SolverInput.model_validate(inp.model_dump(mode="json"))


# ---------------------------------------------------------------- the oracle


def _shortfalls(out: SolverOutput) -> dict[str, int]:
    if out.infeasibility is None:
        return {}
    return {
        v.project_id: v.shortfall_minutes
        for v in out.infeasibility.violations
        if isinstance(v, DemandShortfall)
    }


def _on_time_minutes(out: SolverOutput, p: ProjectInput, inp: SolverInput) -> int:
    """Minutes of the project's blocks that count toward its demand (before its deadline)."""
    end = inp.horizon.end
    if p.weekly_target_minutes is None and p.deadline is not None:
        end = min(end, p.deadline)
    return sum(
        max(0, _minutes(min(b.span.end, end) - b.span.start))
        for b in out.blocks
        if b.project_id == p.id
    )


def _apply(inp: SolverInput, r: Relaxation) -> SolverInput:
    def patch(**fields: object) -> tuple[ProjectInput, ...]:
        return tuple(
            p.model_copy(update=fields) if p.id == r.project_id else p for p in inp.projects
        )

    if r.kind == "extend_deadline":
        assert r.target_date is not None
        p = next(q for q in inp.projects if q.id == r.project_id)
        assert p.target_date is not None and p.deadline is not None
        shift = timedelta(days=(r.target_date - p.target_date).days)
        update = {"projects": patch(target_date=r.target_date, deadline=p.deadline + shift)}
    elif r.kind == "reduce_estimate":
        update = {"projects": patch(remaining_minutes=r.remaining_minutes)}
    elif r.kind == "reduce_weekly_target":
        update = {"projects": patch(weekly_target_minutes=r.weekly_target_minutes)}
    elif r.kind == "raise_max_block_minutes":
        update = {"projects": patch(max_block_minutes=r.max_block_minutes)}
    elif r.kind == "pause_project":
        update = {
            "projects": tuple(p for p in inp.projects if p.id != r.project_id),
            "pins": tuple(x for x in inp.pins if x.project_id != r.project_id),
        }
    elif r.kind == "remove_pin":
        update = {"pins": tuple(x for x in inp.pins if x.id != r.pin_id)}
    else:
        update = {"exceptions": tuple(x for x in inp.exceptions if x.id != r.exception_id)}
    return SolverInput.model_validate(inp.model_copy(update=update).model_dump(mode="json"))


def _check(inp: SolverInput) -> None:
    out = pack(inp)

    # 1. verify is the oracle.
    assert verify(out.blocks, inp) == []

    inf = out.infeasibility
    violations = inf.violations if inf else ()
    pins = {p.id: p for p in inp.pins}

    # 2. Every PinInvalidated names exactly one input pin, matching it, and reports each
    #    (pin, conflict, exception) once; every unrealized pin is reported, never dropped.
    reports = [v for v in violations if isinstance(v, PinInvalidated)]
    keys = [(v.pin_id, v.conflicts_with, v.exception_id) for v in reports]
    assert len(keys) == len(set(keys))
    for v in reports:
        pin = pins[v.pin_id]
        assert (v.pin_kind, v.project_id, v.span) == (pin.kind, pin.project_id, pin.span)
    reported = {v.pin_id for v in reports}
    raw_ids = {i for x in (inf.verify_violations if inf else ()) for i in x.related_ids}
    realized = {(b.project_id, b.span) for b in out.blocks if b.pinned}
    realized |= {(f.project_id, f.span) for f in inp.frozen}
    for pin in inp.pins:
        if pin.kind == "pin" and (pin.project_id, pin.span) not in realized:
            assert pin.id in reported or pin.id in raw_ids, f"pin {pin.id} silently dropped"

    # 3. Every shortfall project is genuinely under-scheduled, and the numbers match the plan.
    projects = {p.id: p for p in inp.projects}
    for v in violations:
        if isinstance(v, DemandShortfall):
            assert v.scheduled_minutes < v.required_minutes
            assert v.scheduled_minutes == _on_time_minutes(out, projects[v.project_id], inp)

    # 4. Each relaxation recovers what it claims when applied and re-packed. `recovers` is the
    #    drop in total shortfall (a reduce_* lowers `required` rather than raising `scheduled`),
    #    so that is what is compared; a paused project's own shortfall is excluded.
    before = _shortfalls(out)
    for r in inf.relaxations if inf else ():
        assert r.recovers_minutes >= 0
        after = _shortfalls(pack(_apply(inp, r)))
        excluded = r.project_id if r.kind == "pause_project" else None
        gained = sum(m - after.get(pid, 0) for pid, m in before.items() if pid != excluded)
        slack = max((p.min_block_minutes for p in inp.projects), default=0)
        assert gained >= r.recovers_minutes - slack, (r, gained)


QUICK = settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
SLOW = settings(max_examples=5000, deadline=None, suppress_health_check=list(HealthCheck))


@QUICK
@given(stress_inputs())
def test_stress_properties(inp: SolverInput) -> None:
    _check(inp)


@pytest.mark.slow
@SLOW
@given(stress_inputs())
def test_stress_properties_5k(inp: SolverInput) -> None:
    _check(inp)


# --------------------------------------------------- two PYTHONHASHSEED values

HASHSEED_CASES = [
    "infeasible_conflicting_pins",
    "resched_no_laptop_tuesday",
    "pack_diversity_focused",
    "rule_frozen_midweek",
    "verify_valid_plan_repack",
]

_PROGRAM = """
import json, sys
from app.scheduler import PlannedBlock, SolverInput, pack
raw = json.loads(open(sys.argv[1]).read())
inp = SolverInput.model_validate(raw["solver_input"])
prev = [PlannedBlock.model_validate(b) for b in raw["previous"]]
sys.stdout.write(pack(inp, prev).model_dump_json())
"""


def _run(seed: str, path: Path) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    result = subprocess.run(
        [sys.executable, "-c", _PROGRAM, str(path)],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


@pytest.mark.parametrize("name", HASHSEED_CASES)
def test_output_is_byte_identical_across_hash_seeds(name: str) -> None:
    path = input_path(name)
    a, b = _run("0", path), _run("12345", path)
    assert a and a == b
    golden = json.loads(expected_path(name).read_text())["output"]
    assert SolverOutput.model_validate_json(a) == SolverOutput.model_validate(golden)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.scheduler import (
    ExceptionInput,
    FrozenBlock,
    Interval,
    Pin,
    PlannedBlock,
    ProjectInput,
    SolverInput,
    ViolationKind,
    verify,
)

from .conftest import fixture_names, load_case

ALL_KINDS = {
    "before_now",
    "block_length",
    "buffer",
    "busy",
    "duplicate_lineage",
    "exception",
    "forbid",
    "horizon",
    "overlap",
    "perennial_daily",
    "pin_missing",
    "pin_unmatched",
    "preferred_window",
    "unknown_project",
    "window",
}


def test_all_kinds_matches_violation_kind_literal() -> None:
    assert set(get_args(ViolationKind)) == ALL_KINDS


# ---------------------------------------------------------------- fixtures


def test_one_fixture_per_violation_kind() -> None:
    names = set(fixture_names())
    assert ALL_KINDS <= names
    assert "valid_plan" in names
    for kind in ALL_KINDS:
        assert load_case(kind).expected_kinds == {kind}


@pytest.mark.parametrize("name", fixture_names())
def test_fixture_yields_exactly_expected_kinds(name: str) -> None:
    case = load_case(name)
    kinds = {v.kind for v in verify(case.plan, case.input)}
    assert kinds == case.expected_kinds, case.description


def test_valid_plan_has_no_violations() -> None:
    case = load_case("valid_plan")
    assert verify(case.plan, case.input) == []


def test_pinned_block_skips_length_but_unpinned_does_not() -> None:
    pinned = load_case("pinned_overlong")
    unpinned = load_case("unpinned_overlong")
    assert verify(pinned.plan, pinned.input) == []
    [violation] = verify(unpinned.plan, unpinned.input)
    assert violation.kind == "block_length"
    assert violation.block_lineage_ids == ("L-long",)


def test_violation_on_pinned_block_reports_pin_id() -> None:
    case = load_case("valid_plan")
    # Calendar busy time now lands on the pinned 09:00-10:00 block: busy beats pins.
    busy = Interval(
        start=datetime(2026, 11, 16, 9, 30, tzinfo=UTC),
        end=datetime(2026, 11, 16, 9, 45, tzinfo=UTC),
    )
    inp = case.input.model_copy(update={"busy": (*case.input.busy, busy)})
    [violation] = verify(case.plan, inp)
    assert violation.kind == "busy"
    assert "pin-1" in violation.related_ids
    assert violation.block_lineage_ids == ("L-pin",)


def test_violations_are_sorted_by_kind_then_lineage() -> None:
    case = load_case("valid_plan")
    t = datetime(2026, 11, 16, tzinfo=UTC)

    def blk(lid: str, pid: str, start: timedelta, end: timedelta, pinned: bool = False):
        return PlannedBlock(
            lineage_id=lid,
            project_id=pid,
            span=Interval(start=t + start, end=t + end),
            pinned=pinned,
        )

    p2 = case.input.projects[1].model_copy(
        update={
            "preferred_windows_strict": True,
            "preferred_windows": (Interval(start=t, end=t + timedelta(days=1)),),
        }
    )
    inp = case.input.model_copy(
        update={"projects": (case.input.projects[0], p2), "now": t + timedelta(hours=8)}
    )
    extra = [
        blk("L-z", "P9", timedelta(hours=23), timedelta(hours=23, minutes=30)),
        blk("L-a", "P2", timedelta(hours=12), timedelta(hours=12, minutes=30)),
        blk("L-early", "P1", timedelta(hours=7), timedelta(hours=7, minutes=30)),
        blk("L-b3", "P2", timedelta(days=2, hours=9), timedelta(days=2, hours=9, minutes=30)),
        blk("L-pz", "P1", timedelta(days=1, hours=14), timedelta(days=1, hours=15), True),
    ]
    violations = verify([*case.plan, *extra], inp)
    keys = [(v.kind, v.block_lineage_ids) for v in violations]
    assert keys == sorted(keys)
    assert {v.kind for v in violations} == {
        "before_now",
        "busy",
        "duplicate_lineage",
        "pin_unmatched",
        "preferred_window",
        "unknown_project",
        "window",
    }


def test_frozen_blocks_count_for_overlap_buffer_and_pins() -> None:
    case = load_case("pin_missing")
    pin = next(p for p in case.input.pins if p.kind == "pin")
    frozen = FrozenBlock(lineage_id="F1", project_id="P1", span=pin.span, state="completed")
    inp = case.input.model_copy(update={"frozen": (frozen,)})
    assert verify(case.plan, inp) == []
    clash = PlannedBlock(lineage_id="L-c", project_id="P2", span=pin.span)
    kinds = {v.kind for v in verify([*case.plan, clash], inp)}
    assert kinds == {"overlap"}


def test_touching_blocks_with_zero_buffer_are_valid() -> None:
    case = load_case("valid_plan")
    inp = case.input.model_copy(update={"buffer_minutes": 0, "pins": ()})
    t = datetime(2026, 11, 17, 9, tzinfo=UTC)
    plan = [
        PlannedBlock(
            lineage_id=f"L{i}",
            project_id="P2",
            span=Interval(
                start=t + timedelta(minutes=30 * i), end=t + timedelta(minutes=30 * i + 30)
            ),
        )
        for i in range(3)
    ]
    assert verify(plan, inp) == []


# --------------------------------------------------- pins: exact endpoints


def _valid() -> tuple[SolverInput, list[PlannedBlock]]:
    case = load_case("valid_plan")
    return case.input, case.plan


def _replace(plan: list[PlannedBlock], lineage_id: str, **update: object) -> list[PlannedBlock]:
    return [b.model_copy(update=update) if b.lineage_id == lineage_id else b for b in plan]


def _span(h0: int, m0: int, h1: int, m1: int, day: int = 16) -> Interval:
    return Interval(
        start=datetime(2026, 11, day, h0, m0, tzinfo=UTC),
        end=datetime(2026, 11, day, h1, m1, tzinfo=UTC),
    )


def test_containing_block_does_not_realize_pin() -> None:
    inp, plan = _valid()
    # Pin is 09:00-10:00; a pinned 08:45-10:00 block contains it but is not an exact match.
    plan = _replace(plan, "L-pin", span=_span(8, 45, 10, 0))
    kinds = {v.kind for v in verify(plan, inp)}
    assert kinds == {"pin_missing", "pin_unmatched"}


def test_block_inside_pin_does_not_realize_pin() -> None:
    inp, plan = _valid()
    plan = _replace(plan, "L-pin", span=_span(9, 0, 9, 45))
    kinds = {v.kind for v in verify(plan, inp)}
    assert kinds == {"pin_missing", "pin_unmatched"}


def test_exact_match_with_pinned_false_is_pin_unmatched() -> None:
    inp, plan = _valid()
    plan = _replace(plan, "L-pin", pinned=False)
    [violation] = verify(plan, inp)
    assert violation.kind == "pin_unmatched"
    assert violation.block_lineage_ids == ("L-pin",)
    assert "pin-1" in violation.related_ids


def test_pinned_flag_without_input_pin_gets_no_length_exemption() -> None:
    case = load_case("pinned_overlong")
    inp = case.input.model_copy(
        update={"pins": tuple(p for p in case.input.pins if p.id != "pin-long")}
    )
    kinds = {v.kind for v in verify(case.plan, inp)}
    assert kinds == {"block_length", "pin_unmatched"}


def test_conflict_on_realized_pin_reports_pin_id_from_inputs_not_flag() -> None:
    inp, plan = _valid()
    busy = _span(9, 30, 9, 45)
    inp = inp.model_copy(update={"busy": (*inp.busy, busy)})
    # Flag is wrong (False) but the block still exactly realizes pin-1.
    violations = verify(_replace(plan, "L-pin", pinned=False), inp)
    busy_v = next(v for v in violations if v.kind == "busy")
    assert "pin-1" in busy_v.related_ids
    # A pinned-flagged block with no matching pin gets no pin id attached.
    moved = _replace(plan, "L-pin", span=_span(12, 15, 12, 45))
    busy_v = next(v for v in verify(moved, inp) if v.kind == "busy")
    assert "pin-1" not in busy_v.related_ids


# ---------------------------------------------------------- immutable past


def test_before_now_flags_planned_but_not_frozen() -> None:
    case = load_case("before_now")
    [violation] = verify(case.plan, case.input)
    assert violation.kind == "before_now"
    assert violation.block_lineage_ids == ("L-x",)
    early = next(b for b in case.plan if b.lineage_id == "L-x")
    frozen = FrozenBlock(lineage_id="F-x", project_id="P2", span=early.span, state="completed")
    inp = case.input.model_copy(update={"frozen": (frozen,)})
    rest = [b for b in case.plan if b.lineage_id != "L-x"]
    assert verify(rest, inp) == []


def test_block_starting_exactly_at_now_is_allowed() -> None:
    inp, plan = _valid()
    inp = inp.model_copy(update={"now": datetime(2026, 11, 16, 10, 30, tzinfo=UTC)})
    kinds = {v.kind for v in verify([b for b in plan if b.lineage_id != "L-pin"], inp)}
    assert "before_now" not in kinds


# ------------------------------------------------------ preferred windows


def test_strict_preferred_windows_merge_adjacent_windows() -> None:
    case = load_case("preferred_window")
    [violation] = verify(case.plan, case.input)
    assert violation.kind == "preferred_window"
    assert violation.block_lineage_ids == ("L-x",)
    assert "P2" in violation.related_ids


def test_non_strict_preferred_windows_produce_nothing() -> None:
    case = load_case("preferred_window")
    projects = tuple(
        p.model_copy(update={"preferred_windows_strict": False}) for p in case.input.projects
    )
    inp = case.input.model_copy(update={"projects": projects})
    assert verify(case.plan, inp) == []


def test_preferred_window_violation_on_realized_pin_reports_pin_id() -> None:
    inp, plan = _valid()
    p1 = inp.projects[0].model_copy(
        update={"preferred_windows_strict": True, "preferred_windows": (_span(14, 0, 16, 0),)}
    )
    inp = inp.model_copy(update={"projects": (p1, inp.projects[1])})
    [violation] = verify(plan, inp)
    assert violation.kind == "preferred_window"
    assert "pin-1" in violation.related_ids


def _strict_evening_p2(inp: SolverInput) -> SolverInput:
    """P2 strict to 22:30-23:30 on the 16th: entirely outside schedulable (08-22)."""
    p2 = inp.projects[1].model_copy(
        update={"preferred_windows_strict": True, "preferred_windows": (_span(22, 30, 23, 30),)}
    )
    return inp.model_copy(update={"projects": (inp.projects[0], p2)})


def test_strict_block_outside_schedulable_but_inside_its_windows_is_valid() -> None:
    inp, plan = _valid()
    inp = _strict_evening_p2(inp)
    plan = [b for b in plan if b.project_id != "P2"] + [
        PlannedBlock(lineage_id="L-eve", project_id="P2", span=_span(22, 30, 23, 30))
    ]
    assert verify(plan, inp) == []


def test_strict_block_outside_its_windows_is_flagged_but_not_as_window() -> None:
    inp, plan = _valid()
    inp = _strict_evening_p2(inp)
    # L-b2 (10:30-11:30) is inside schedulable but outside P2's strict windows.
    kinds = {(v.kind, v.block_lineage_ids) for v in verify(plan, inp)}
    assert ("preferred_window", ("L-b2",)) in kinds
    assert not any(k == "window" for k, _ in kinds)
    # Outside both: only preferred_window, since strict windows replace schedulable.
    plan = _replace(plan, "L-b2", span=_span(23, 0, 23, 45))
    violations = [v for v in verify(plan, inp) if "L-b2" in v.block_lineage_ids]
    assert [v.kind for v in violations] == ["preferred_window"]


def test_non_strict_block_outside_schedulable_is_still_a_window_violation() -> None:
    inp, plan = _valid()
    p2 = inp.projects[1].model_copy(update={"preferred_windows": (_span(22, 30, 23, 30),)})
    inp = inp.model_copy(update={"projects": (inp.projects[0], p2)})
    plan = _replace(plan, "L-b3", span=_span(22, 30, 23, 30))
    [violation] = verify(plan, inp)
    assert violation.kind == "window"


def test_strict_block_still_checked_for_busy_exception_forbid() -> None:
    inp, plan = _valid()
    inp = _strict_evening_p2(inp)
    p2 = inp.projects[1].model_copy(
        update={"requirement_tags": ("laptop",), "preferred_windows": (_span(18, 0, 23, 30),)}
    )
    inp = inp.model_copy(
        update={"projects": (inp.projects[0], p2), "busy": (*inp.busy, _span(22, 30, 23, 0))}
    )
    plan = [
        *(b for b in plan if b.project_id != "P2"),
        PlannedBlock(lineage_id="L-exc", project_id="P2", span=_span(18, 0, 18, 45)),
        PlannedBlock(lineage_id="L-fb", project_id="P2", span=_span(20, 30, 21, 0)),
        PlannedBlock(lineage_id="L-busy", project_id="P2", span=_span(22, 30, 23, 15)),
    ]
    kinds = {(v.kind, v.block_lineage_ids) for v in verify(plan, inp)}
    assert kinds == {
        ("exception", ("L-exc",)),
        ("forbid", ("L-fb",)),
        ("busy", ("L-busy",)),
    }


def test_strict_pin_outside_schedulable_is_realizable_so_missing_is_reported() -> None:
    inp, plan = _valid()
    inp = _strict_evening_p2(inp)
    pin = Pin(id="pin-eve", kind="pin", span=_span(22, 30, 23, 30), project_id="P2")
    inp = inp.model_copy(update={"pins": (*inp.pins, pin)})
    plan = [b for b in plan if b.project_id != "P2"]
    [violation] = verify(plan, inp)
    assert violation.kind == "pin_missing"
    assert "pin-eve" in violation.related_ids
    # A non-strict project's pin there stays unrealizable (outside schedulable): not missing.
    p2 = inp.projects[1].model_copy(update={"preferred_windows_strict": False})
    assert verify(plan, inp.model_copy(update={"projects": (inp.projects[0], p2)})) == []


# --------------------------------------------------------- lineage identity


def test_duplicate_lineage_among_planned_blocks() -> None:
    case = load_case("duplicate_lineage")
    [violation] = verify(case.plan, case.input)
    assert violation.kind == "duplicate_lineage"
    assert violation.block_lineage_ids == ("L-b2",)


def test_planned_block_reusing_frozen_lineage() -> None:
    inp, plan = _valid()
    frozen = FrozenBlock(
        lineage_id="L-b3", project_id="P2", span=_span(8, 0, 8, 30), state="completed"
    )
    inp = inp.model_copy(update={"frozen": (frozen,)})
    [violation] = verify(plan, inp)
    assert violation.kind == "duplicate_lineage"
    assert violation.block_lineage_ids == ("L-b3",)


# -------------------------------------------------------------- properties

BASE = datetime(2026, 11, 16, tzinfo=UTC)
HORIZON_MINUTES = 7 * 24 * 60
TAGS = ("gym", "laptop", "quiet")


def _iv(start_min: int, end_min: int) -> Interval:
    return Interval(
        start=BASE + timedelta(minutes=start_min), end=BASE + timedelta(minutes=end_min)
    )


@st.composite
def valid_scenarios(draw: st.DrawFn) -> tuple[SolverInput, list[PlannedBlock]]:
    """A SolverInput plus a plan that satisfies every constraint by construction."""
    buffer = draw(st.integers(0, 30))
    n_projects = draw(st.integers(1, 3))
    project_tags = [
        draw(st.lists(st.sampled_from(TAGS[:2]), max_size=2, unique=True))
        for _ in range(n_projects)
    ]

    # Lay segments left to right on a minute timeline; every gap honours the buffer.
    specs = draw(
        st.lists(
            st.tuples(
                st.integers(0, 240),  # extra gap beyond the buffer
                st.integers(15, 180),  # length
                st.integers(0, n_projects - 1),  # project
                st.sampled_from(["free", "pinned", "frozen"]),
            ),
            max_size=12,
        )
    )
    cursor = 0
    segments: list[tuple[int, int, int, str]] = []
    for i, (extra, length, proj, mode) in enumerate(specs):
        start = cursor + extra + (buffer if i else 0)
        if start + length > HORIZON_MINUTES:
            break
        segments.append((start, start + length, proj, mode))
        cursor = start + length

    # Block length bounds: every free block of a project fits [min, max]; pins/frozen ignore them.
    projects = []
    for p in range(n_projects):
        lengths = [e - s for s, e, proj, mode in segments if proj == p and mode == "free"]
        lo = min(lengths, default=60)
        hi = max(lengths, default=60)
        min_block = draw(st.integers(1, lo))
        max_block = draw(st.integers(hi, hi + 120))
        # A perennial may not share a day between an ordinary block and any other of its
        # blocks (perennial_daily); pinned and frozen blocks alone may share.
        per_day: dict[int, list[str]] = {}
        for s, _, proj, mode in segments:
            if proj == p:
                per_day.setdefault(s // (24 * 60), []).append(mode)
        may_be_perennial = all(len(m) < 2 or "free" not in m for m in per_day.values())
        weekly = st.none() | st.integers(0, 3000) if may_be_perennial else st.none()
        projects.append(
            ProjectInput(
                id=f"P{p}",
                remaining_minutes=draw(st.integers(0, 6000)),
                weekly_target_minutes=draw(weekly),
                max_block_minutes=max_block,
                min_block_minutes=min_block,
                requirement_tags=tuple(project_tags[p]),
            )
        )

    plan: list[PlannedBlock] = []
    frozen: list[FrozenBlock] = []
    pins: list[Pin] = []
    schedulable: list[Interval] = []
    for i, (s, e, proj, mode) in enumerate(segments):
        if mode == "frozen":
            frozen.append(
                FrozenBlock(
                    lineage_id=f"F{i}", project_id=f"P{proj}", span=_iv(s, e), state="completed"
                )
            )
            continue
        pinned = mode == "pinned"
        plan.append(
            PlannedBlock(lineage_id=f"L{i}", project_id=f"P{proj}", span=_iv(s, e), pinned=pinned)
        )
        if pinned:
            # A pinned block realizes its pin by exact endpoints.
            pins.append(Pin(id=f"pin{i}", kind="pin", span=_iv(s, e), project_id=f"P{proj}"))
        # Windows cover each block with random slack; overlapping windows are allowed.
        schedulable.append(
            _iv(
                max(0, s - draw(st.integers(0, 60))),
                min(HORIZON_MINUTES, e + draw(st.integers(0, 60))),
            )
        )

    # Busy time, blanket forbids, and blocks_all exceptions go only in gaps; tag exceptions
    # may cover blocks whose project lacks the tag.
    planned_spans = [(b.span, b.project_id) for b in plan]
    bounds = [0, *[x for s, e, _, _ in segments for x in (s, e)], HORIZON_MINUTES]
    busy: list[Interval] = []
    exceptions: list[ExceptionInput] = []
    for g in range(0, len(bounds), 2):
        gs, ge = bounds[g], bounds[g + 1]
        if ge - gs < 1:
            continue
        a = draw(st.integers(gs, ge - 1))
        b = draw(st.integers(a + 1, ge))
        what = draw(st.sampled_from(["none", "busy", "forbid", "exception"]))
        if what == "busy":
            busy.append(_iv(a, b))
        elif what == "forbid":
            pins.append(Pin(id=f"forbid{g}", kind="forbid", span=_iv(a, b)))
        elif what == "exception":
            exceptions.append(ExceptionInput(id=f"exc{g}", span=_iv(a, b), blocks_all=True))
    for j, (span, pid) in enumerate(planned_spans):
        if draw(st.booleans()):
            own = set(project_tags[int(pid[1:])])
            safe = [t for t in TAGS if t not in own]
            exceptions.append(ExceptionInput(id=f"texc{j}", span=span, tags=(safe[0],)))
        if draw(st.booleans()):
            other = (int(pid[1:]) + 1) % (n_projects + 1)
            pins.append(Pin(id=f"oforbid{j}", kind="forbid", span=span, project_id=f"P{other}"))

    inp = SolverInput(
        solver_version="test",
        now=BASE,
        horizon=_iv(0, HORIZON_MINUTES),
        busy=tuple(busy),
        schedulable=tuple(schedulable),
        frozen=tuple(frozen),
        pins=tuple(pins),
        projects=tuple(projects),
        exceptions=tuple(exceptions),
        day_diversity=draw(st.sampled_from(["focused", "mixed"])),
        buffer_minutes=buffer,
    )
    return inp, plan


@settings(max_examples=300, deadline=None)
@given(valid_scenarios())
def test_valid_by_construction_plans_verify_clean(
    scenario: tuple[SolverInput, list[PlannedBlock]],
) -> None:
    inp, plan = scenario
    assert verify(plan, inp) == []


@st.composite
def arbitrary_plans(draw: st.DrawFn, inp: SolverInput) -> list[PlannedBlock]:
    project_ids = [p.id for p in inp.projects] + ["P-unknown"]
    n = draw(st.integers(0, 10))
    plan = []
    for i in range(n):
        s = draw(st.integers(-120, HORIZON_MINUTES + 60))
        e = s + draw(st.integers(1, 300))
        plan.append(
            PlannedBlock(
                lineage_id=draw(st.sampled_from([f"L{i}", "L-dup"])),
                project_id=draw(st.sampled_from(project_ids)),
                span=_iv(s, e),
                pinned=draw(st.booleans()),
            )
        )
    return plan


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_verify_is_deterministic_and_order_independent(data: st.DataObject) -> None:
    inp, valid_plan = data.draw(valid_scenarios())
    plan = valid_plan + data.draw(arbitrary_plans(inp))
    shuffled = data.draw(st.permutations(plan))
    first = verify(plan, inp)
    assert verify(plan, inp) == first
    assert verify(list(shuffled), inp) == first
    keys = [(v.kind, v.block_lineage_ids) for v in first]
    assert keys == sorted(keys)


# ------------------------------------- pin attribution on identity/pairwise


def test_duplicate_lineage_with_pin_realization_reports_pin_id() -> None:
    inp, plan = _valid()
    # The ordinary 10:30 block takes the pin block's lineage.
    plan = [
        b.model_copy(update={"lineage_id": "L-pin"}) if b.lineage_id == "L-b2" else b for b in plan
    ]
    [violation] = verify(plan, inp)
    assert violation.kind == "duplicate_lineage"
    assert violation.block_lineage_ids == ("L-pin",)
    assert "pin-1" in violation.related_ids


def test_duplicate_lineage_with_frozen_pin_realization_reports_pin_id() -> None:
    inp, plan = _valid()
    plan = [b for b in plan if b.lineage_id != "L-pin"]
    frozen = FrozenBlock(
        lineage_id="L-b3", project_id="P1", span=_span(9, 0, 10, 0), state="completed"
    )
    inp = inp.model_copy(update={"frozen": (frozen,)})
    [violation] = verify(plan, inp)
    assert violation.kind == "duplicate_lineage"
    assert "pin-1" in violation.related_ids


def test_frozen_pin_realization_overlapping_planned_block_lists_pin_id() -> None:
    inp, plan = _valid()
    plan = [b for b in plan if b.lineage_id != "L-pin"]
    frozen = FrozenBlock(
        lineage_id="F-pin", project_id="P1", span=_span(9, 0, 10, 0), state="completed"
    )
    inp = inp.model_copy(update={"frozen": (frozen,)})
    clash = PlannedBlock(lineage_id="L-c", project_id="P2", span=_span(9, 30, 10, 0))
    near = PlannedBlock(lineage_id="L-n", project_id="P2", span=_span(8, 0, 8, 55))
    violations = verify([*plan, clash, near], inp)
    by_kind = {v.kind: v for v in violations}
    assert set(by_kind) >= {"overlap", "buffer"}
    assert "pin-1" in by_kind["overlap"].related_ids
    assert set(by_kind["overlap"].block_lineage_ids) == {"F-pin", "L-c"}
    buf = next(v for v in violations if v.kind == "buffer" and "F-pin" in v.block_lineage_ids)
    assert "pin-1" in buf.related_ids


# ------------------------------------------- pin_missing only for realizable pins


def test_unrealizable_pin_is_not_pin_missing() -> None:
    """ARCH §3: a pin invalidated by calendar/exceptions is reported, not placed. A plan that
    leaves it out must still verify, or no plan for that input could ever be persisted."""
    inp, plan = _valid()
    inp = inp.model_copy(update={"busy": (*inp.busy, _span(9, 30, 9, 45))})
    assert verify([b for b in plan if b.lineage_id != "L-pin"], inp) == []


def test_pin_conflicting_only_with_ordinary_block_is_still_missing() -> None:
    inp, plan = _valid()
    plan = [b for b in plan if b.lineage_id != "L-pin"]
    squatter = PlannedBlock(lineage_id="L-sq", project_id="P2", span=_span(9, 0, 10, 0))
    kinds = {v.kind for v in verify([*plan, squatter], inp)}
    assert kinds == {"pin_missing"}


def test_pin_conflicting_with_another_realized_pin_is_not_missing() -> None:
    inp, plan = _valid()
    rival = Pin(id="pin-2", kind="pin", span=_span(9, 30, 10, 30), project_id="P2")
    inp = inp.model_copy(update={"pins": (*inp.pins, rival)})
    assert verify(plan, inp) == []
    # Neither placed: both are realizable on their own, so both are missing.
    rest = [b for b in plan if b.lineage_id != "L-pin"]
    missing = [v for v in verify(rest, inp) if v.kind == "pin_missing"]
    assert {i for v in missing for i in v.related_ids} >= {"pin-1", "pin-2"}


# ------------------------------------- perennial_daily: binds the solver, not the user


def _daily_case(*members: tuple[str, int, int, str]) -> list:  # type: ignore[type-arg]
    """Blocks of perennial P2 on Monday from the perennial_daily fixture's input.
    Each member is (lineage, start hour, end hour, mode) with mode ordinary|pinned|frozen."""
    case = load_case("perennial_daily")
    day = datetime(2026, 11, 16, tzinfo=UTC)
    plan: list[PlannedBlock] = []
    frozen: list[FrozenBlock] = []
    pins: list[Pin] = [p for p in case.input.pins if p.kind == "forbid"]
    for lid, h0, h1, mode in members:
        span = Interval(start=day + timedelta(hours=h0), end=day + timedelta(hours=h1))
        if mode == "frozen":
            frozen.append(
                FrozenBlock(lineage_id=lid, project_id="P2", span=span, state="completed")
            )
            continue
        plan.append(
            PlannedBlock(lineage_id=lid, project_id="P2", span=span, pinned=mode == "pinned")
        )
        if mode == "pinned":
            pins.append(Pin(id=f"pin-{lid}", kind="pin", span=span, project_id="P2"))
    inp = case.input.model_copy(update={"now": day, "pins": tuple(pins), "frozen": tuple(frozen)})
    return [v.kind for v in verify(plan, inp)]


@pytest.mark.parametrize(
    ("modes", "flagged"),
    [
        (("pinned", "pinned"), False),
        (("pinned", "frozen"), False),
        (("frozen", "frozen"), False),
        (("ordinary", "pinned"), True),
        (("ordinary", "frozen"), True),
        (("ordinary", "ordinary"), True),
    ],
)
def test_perennial_daily_binds_the_solver_not_the_user(
    modes: tuple[str, str], flagged: bool
) -> None:
    kinds = _daily_case(("L-a", 9, 10, modes[0]), ("L-b", 14, 15, modes[1]))
    assert ("perennial_daily" in kinds) is flagged
    assert set(kinds) <= {"perennial_daily"}


def test_perennial_daily_days_start_at_horizon_start() -> None:
    # 22:00-23:00 Monday and 00:15-01:00 Tuesday are different days.
    case = load_case("perennial_daily")
    mon = datetime(2026, 11, 16, tzinfo=UTC)
    plan = [
        PlannedBlock(
            lineage_id="L-late",
            project_id="P2",
            span=Interval(start=mon + timedelta(hours=22), end=mon + timedelta(hours=23)),
        ),
        PlannedBlock(
            lineage_id="L-next",
            project_id="P2",
            span=Interval(
                start=mon + timedelta(hours=24, minutes=15), end=mon + timedelta(hours=25)
            ),
        ),
    ]
    schedulable = (Interval(start=mon, end=mon + timedelta(days=2)),)
    inp = case.input.model_copy(update={"now": mon, "schedulable": schedulable, "pins": ()})
    assert verify(plan, inp) == []

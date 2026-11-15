"""Hypothesis strategies for random, possibly infeasible, SolverInputs (7-day horizon)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from hypothesis import strategies as st

from app.scheduler import ExceptionInput, FrozenBlock, Interval, Pin, ProjectInput, SolverInput

BASE = datetime(2026, 11, 16, tzinfo=UTC)
HORIZON_MINUTES = 7 * 24 * 60
TAGS = ("gym", "laptop", "quiet")


def iv(start_min: int, end_min: int) -> Interval:
    return Interval(
        start=BASE + timedelta(minutes=start_min), end=BASE + timedelta(minutes=end_min)
    )


@st.composite
def spans(draw: st.DrawFn, lo: int = -600, hi: int = HORIZON_MINUTES + 600) -> Interval:
    s = draw(st.integers(lo, hi))
    return iv(s, s + draw(st.integers(1, 600)))


@st.composite
def solver_inputs(draw: st.DrawFn) -> SolverInput:
    n = draw(st.integers(1, 6))
    projects = []
    for i in range(n):
        lo = draw(st.integers(1, 120))
        hi = draw(st.integers(lo, 240))
        windows = tuple(draw(st.lists(spans(0, HORIZON_MINUTES), max_size=3)))
        perennial = draw(st.booleans())
        target = draw(st.none() | st.dates(date(2026, 11, 10), date(2027, 1, 31)))
        # The caller projects end-of-day through the user's tz; a random offset stands in.
        offset = timedelta(hours=draw(st.integers(-14, 12)))
        deadline = (
            None
            if target is None
            else datetime.combine(target + timedelta(days=1), datetime.min.time(), UTC) - offset
        )
        projects.append(
            ProjectInput(
                id=f"P{i}",
                remaining_minutes=draw(st.integers(0, 3000)),
                weekly_target_minutes=draw(st.integers(0, 1500)) if perennial else None,
                max_block_minutes=hi,
                min_block_minutes=lo,
                preferred_windows=windows,
                preferred_windows_strict=bool(windows) and draw(st.booleans()),
                requirement_tags=tuple(draw(st.lists(st.sampled_from(TAGS), max_size=2))),
                target_date=target,
                deadline=deadline,
            )
        )
    pid = st.sampled_from([p.id for p in projects] + ["P-gone"])

    # Wake windows per day (some days absent), with random jitter.
    schedulable = []
    for d in range(-1, 8):
        if draw(st.integers(0, 9)) == 0:
            continue
        a = d * 1440 + draw(st.integers(5 * 60, 10 * 60))
        b = d * 1440 + draw(st.integers(17 * 60, 24 * 60))
        schedulable.append(iv(a, b))

    now = BASE + timedelta(minutes=draw(st.integers(-120, 3 * 1440)))
    frozen = []
    for i in range(draw(st.integers(0, 3))):
        s = draw(st.integers(-300, HORIZON_MINUTES))
        frozen.append(
            FrozenBlock(
                lineage_id=f"F{i}",
                project_id=draw(pid),
                span=iv(s, s + draw(st.integers(15, 180))),
                state=draw(st.sampled_from(["planned", "completed", "skipped"])),
            )
        )
    pins = []
    for i in range(draw(st.integers(0, 5))):
        kind = draw(st.sampled_from(["pin", "forbid"]))
        if kind == "pin":
            span = draw(spans(-200, HORIZON_MINUTES))
            span = iv(*[((t - BASE) // timedelta(minutes=1)) for t in (span.start, span.end)])
            pins.append(
                Pin(
                    id=f"pin{i}",
                    kind="pin",
                    span=span,
                    project_id=draw(pid),
                    created_from_lineage_id=draw(st.none() | st.sampled_from(["L-old", "F0"])),
                )
            )
        else:
            pins.append(
                Pin(
                    id=f"forbid{i}",
                    kind="forbid",
                    span=draw(spans()),
                    project_id=draw(st.none() | pid),
                )
            )
    exceptions = []
    for i in range(draw(st.integers(0, 3))):
        blocks_all = draw(st.booleans())
        tags = tuple(
            draw(st.lists(st.sampled_from(TAGS), min_size=0 if blocks_all else 1, max_size=2))
        )
        exceptions.append(
            ExceptionInput(id=f"exc{i}", span=draw(spans()), tags=tags, blocks_all=blocks_all)
        )
    return SolverInput(
        solver_version="test",
        now=now,
        horizon=iv(0, HORIZON_MINUTES),
        busy=tuple(draw(st.lists(spans(), max_size=8))),
        schedulable=tuple(schedulable),
        frozen=tuple(frozen),
        pins=tuple(pins),
        projects=tuple(projects),
        exceptions=tuple(exceptions),
        day_diversity=draw(st.sampled_from(["focused", "mixed"])),
        buffer_minutes=draw(st.integers(0, 30)),
    )

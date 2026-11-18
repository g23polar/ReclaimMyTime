"""Regenerate golden expected files (never run by the test suite).

    cd server
    uv run python tests/scheduler/tools/regen_goldens.py               # rewrite all expected
    uv run python tests/scheduler/tools/regen_goldens.py --only NAME   # one entry (repeatable)
    uv run python tests/scheduler/tools/regen_goldens.py --seed-inputs # add missing inputs

Expected files are a pure function of the input file and the current solver, so running this
twice is a no-op. `--seed-inputs` writes only *missing* `*.input.json` files from the
builders below (fixtures + hand-built scenarios); it never overwrites a stored input, because
a stored input is a snapshot, not a derived artifact. A seeded input whose plan fails
`verify()` is refused and reported instead of being written.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SERVER_DIR))

from app.scheduler import (  # noqa: E402
    ExceptionInput,
    FrozenBlock,
    Interval,
    Pin,
    PlannedBlock,
    ProjectInput,
    SolverInput,
    pack,
    verify,
)
from tests.scheduler.golden.corpus_io import (  # noqa: E402
    corpus_names,
    expected_names,
    expected_path,
    input_path,
    replay_rendered,
)

FIXTURES_DIR = SERVER_DIR / "tests" / "scheduler" / "fixtures"
MONDAY = datetime(2026, 11, 16, tzinfo=UTC)


# ------------------------------------------------------------------ regeneration


def regenerate(only: list[str]) -> int:
    names = corpus_names()
    unknown = sorted(set(only) - set(names))
    if unknown:
        print(f"unknown corpus entries: {', '.join(unknown)}", file=sys.stderr)
        return 2
    changed = 0
    for name in only or names:
        path = expected_path(name)
        new = replay_rendered(name)
        old = path.read_text() if path.exists() else None
        if old == new:
            continue
        path.write_text(new)
        changed += 1
        print(f"{'new' if old is None else 'changed'}: {name}")
    for orphan in sorted(set(expected_names()) - set(names)):
        print(f"orphan expected file (no input): {orphan}", file=sys.stderr)
    print(f"{changed} changed, {len(only or names) - changed} unchanged")
    return 0


# ---------------------------------------------------------------- input seeding


def t(day: int, h: int, m: int = 0) -> datetime:
    return MONDAY + timedelta(days=day, hours=h, minutes=m)


def span(day: int, h0: int, h1: int, m0: int = 0, m1: int = 0) -> Interval:
    return Interval(start=t(day, h0, m0), end=t(day, h1, m1))


def week(h0: int = 8, h1: int = 20, days: int = 7) -> tuple[Interval, ...]:
    return tuple(span(d, h0, h1) for d in range(days))


def eod(d: date) -> datetime:
    """End of a local date, for a UTC user."""
    return datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC)


def deadline_project(pid: str, remaining: int, due: date, **kw: object) -> ProjectInput:
    return ProjectInput(
        id=pid,
        remaining_minutes=remaining,
        max_block_minutes=kw.pop("max_block_minutes", 120),  # type: ignore[arg-type]
        min_block_minutes=kw.pop("min_block_minutes", 60),  # type: ignore[arg-type]
        target_date=due,
        deadline=eod(due),
        **kw,  # type: ignore[arg-type]
    )


def perennial(pid: str, weekly: int, **kw: object) -> ProjectInput:
    return ProjectInput(
        id=pid,
        remaining_minutes=0,
        weekly_target_minutes=weekly,
        max_block_minutes=kw.pop("max_block_minutes", 60),  # type: ignore[arg-type]
        min_block_minutes=kw.pop("min_block_minutes", 30),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def solver_input(**kw: object) -> SolverInput:
    base: dict[str, object] = {
        "solver_version": "golden",
        "now": MONDAY,
        "horizon": Interval(start=MONDAY, end=t(7, 0)),
        "schedulable": week(),
        "day_diversity": "mixed",
        "buffer_minutes": 15,
    }
    return SolverInput.model_validate({**base, **kw})


Seed = tuple[str, SolverInput, list[PlannedBlock]]


def fixture_input(path: Path) -> tuple[str, SolverInput, list[PlannedBlock]]:
    raw = json.loads(path.read_text())
    plan = [PlannedBlock.model_validate(b) for b in raw.get("plan", [])]
    return raw["description"], SolverInput.model_validate(raw["input"]), plan


def advance(
    inp: SolverInput, now: datetime, **update: object
) -> tuple[SolverInput, list[PlannedBlock]]:
    """Mid-week re-solve (PRD §6): the first plan's past becomes frozen, the rest `previous`.

    Completed deadline work is subtracted from `remaining_minutes`, as the caller would.
    """
    plan = pack(inp).blocks
    frozen = [
        FrozenBlock(
            lineage_id=b.lineage_id,
            project_id=b.project_id,
            span=b.span,
            state="completed" if b.span.end <= now else "planned",
        )
        for b in plan
        if b.span.start < now
    ]
    done: dict[str, int] = {}
    for f in frozen:
        if f.state == "completed":
            done[f.project_id] = done.get(f.project_id, 0) + f.span.duration_minutes
    projects = tuple(
        p
        if p.weekly_target_minutes is not None
        else p.model_copy(
            update={"remaining_minutes": max(0, p.remaining_minutes - done.get(p.id, 0))}
        )
        for p in inp.projects
    )
    later = inp.model_copy(update={"now": now, "frozen": tuple(frozen), "projects": projects})
    later = SolverInput.model_validate(later.model_dump(mode="json") | update)
    return later, [b for b in plan if b.span.start >= now]


def _fixture_seeds() -> list[tuple[str, Callable[[], Seed]]]:
    seeds: list[tuple[str, Callable[[], Seed]]] = []
    for path in sorted((FIXTURES_DIR / "pack").glob("*.json")):

        def build(path: Path = path) -> Seed:
            desc, inp, _ = fixture_input(path)
            return f"pack fixture {path.stem}: {desc}", inp, []

        seeds.append((f"pack_{path.stem}", build))
    # Verify fixtures: 13 share one input; keep the distinct ones, plus one re-pack of
    # valid_plan's plan as `previous` (its lineages should be reused).
    for stem in ("valid_plan", "before_now", "horizon", "pinned_overlong", "preferred_window"):

        def build_v(stem: str = stem) -> Seed:
            desc, inp, _ = fixture_input(FIXTURES_DIR / f"{stem}.json")
            return f"verify fixture {stem} input: {desc}", inp, []

        seeds.append((f"verify_{stem}", build_v))

    def build_repack() -> Seed:
        _, inp, plan = fixture_input(FIXTURES_DIR / "valid_plan.json")
        return "verify valid_plan input re-packed with its plan as previous.", inp, plan

    seeds.append(("verify_valid_plan_repack", build_repack))
    return seeds


def _rule_seeds() -> dict[str, Callable[[], Seed]]:
    sun = date(2026, 11, 22)

    def spacing() -> Seed:
        inp = solver_input(projects=[deadline_project("essay", 600, sun)])
        return "PRD §5.2 spacing: one 10h deadline project spreads over the week.", inp, []

    def pressure() -> Seed:
        inp = solver_input(
            projects=[
                deadline_project("course", 1800, date(2026, 12, 6)),
                deadline_project("taxes", 240, date(2026, 11, 18), max_block_minutes=90),
                perennial("run", 180),
            ]
        )
        return (
            "PRD §5.2 deadline pressure: 30h due in 3 weeks -> 10h this week; a near "
            "deadline (Wed) is placed before it.",
            inp,
            [],
        )

    def rhythm() -> Seed:
        mornings = tuple(span(d, 6, 9) for d in range(7))
        inp = solver_input(
            schedulable=week(6, 22),
            projects=[
                perennial("workout", 300, preferred_windows=mornings),
                perennial("reading", 210, min_block_minutes=30, max_block_minutes=45),
            ],
        )
        return "PRD §5.2 perennial rhythm: workout mornings (non-strict window).", inp, []

    def buffer_min_wake() -> Seed:
        busy = []
        for d in range(5):
            busy += [span(d, 9, 10), span(d, 10, 11, 40), span(d, 12, 14, 50), span(d, 16, 17)]
        inp = solver_input(
            schedulable=week(9, 17, days=5),
            busy=busy,
            buffer_minutes=15,
            projects=[deadline_project("report", 900, sun, min_block_minutes=45)],
        )
        return (
            "PRD §5.2 buffers + min block + wake windows: weekday 09-17 only, gaps of "
            "40/70/120 min, 15 min buffer, 45 min minimum.",
            inp,
            [],
        )

    def strict_window() -> Seed:
        evenings = tuple(span(d, 18, 21) for d in range(7))
        inp = solver_input(
            schedulable=week(8, 22),
            projects=[
                perennial(
                    "guitar",
                    240,
                    preferred_windows=evenings,
                    preferred_windows_strict=True,
                    max_block_minutes=90,
                ),
                deadline_project("deck", 480, sun),
            ],
        )
        return "Strict preferred window: guitar only 18-21.", inp, []

    def strict_window_outside_availability() -> Seed:
        evenings = tuple(span(d, 18, 22) for d in range(7))
        inp = solver_input(
            schedulable=week(9, 18),
            busy=[span(1, 18, 20)],
            projects=[
                perennial(
                    "read",
                    240,
                    preferred_windows=evenings,
                    preferred_windows_strict=True,
                ),
                perennial(
                    "sketch",
                    120,
                    preferred_windows=evenings,
                    preferred_windows_strict=False,
                ),
                deadline_project("deck", 480, sun),
            ],
        )
        return (
            "Strict windows replace availability (2026-11-17): work hours 09-18, 'read' "
            "strict 18-22 is scheduled in the evenings; 'sketch' with the same windows "
            "non-strict stays inside 09-18.",
            inp,
            [],
        )

    def frozen_midweek() -> Seed:
        inp = solver_input(
            now=t(2, 13, 7),
            frozen=[
                FrozenBlock(
                    lineage_id="F-mon",
                    project_id="essay",
                    span=span(0, 9, 11),
                    state="completed",
                ),
                FrozenBlock(
                    lineage_id="F-tue", project_id="gym", span=span(1, 8, 9), state="skipped"
                ),
                FrozenBlock(
                    lineage_id="F-wed", project_id="essay", span=span(2, 12, 14), state="planned"
                ),
                FrozenBlock(
                    lineage_id="F-gym", project_id="gym", span=span(0, 8, 9), state="completed"
                ),
            ],
            projects=[deadline_project("essay", 480, sun), perennial("gym", 240)],
        )
        return (
            "Wednesday 13:07 (off-grid now), frozen completed/skipped/in-progress blocks: "
            "skipped time does not count, in-progress does.",
            inp,
            [],
        )

    def focused_mix() -> Seed:
        inp = solver_input(
            day_diversity="focused",
            projects=[
                deadline_project("app", 600, sun),
                deadline_project("paper", 480, date(2026, 11, 29)),
                perennial("piano", 180),
                perennial("spanish", 150, max_block_minutes=30),
            ],
        )
        return "focused day diversity with a perennial + deadline mix.", inp, []

    return {
        "rule_spacing_deadline": spacing,
        "rule_deadline_pressure_multiweek": pressure,
        "rule_perennial_rhythm": rhythm,
        "rule_buffer_min_block_wake": buffer_min_wake,
        "rule_strict_preferred_window": strict_window,
        "rule_strict_window_outside_availability": strict_window_outside_availability,
        "rule_frozen_midweek": frozen_midweek,
        "rule_focused_perennial_deadline_mix": focused_mix,
    }


def _resched_seeds() -> dict[str, Callable[[], Seed]]:
    def baseline() -> SolverInput:
        return fixture_input(FIXTURES_DIR / "pack" / "no_laptop_baseline.json")[1]

    def no_laptop() -> Seed:
        exc = ExceptionInput(
            id="no-laptop", span=Interval(start=t(1, 10), end=t(3, 0)), tags=("laptop",)
        )
        inp, prev = advance(baseline(), t(1, 10), exceptions=[exc.model_dump(mode="json")])
        return "PRD §6 canonical: Tue 10:00 'no laptop today or tomorrow'.", inp, prev

    def calendar_moved() -> Seed:
        inp = fixture_input(FIXTURES_DIR / "pack" / "lineage_reuse.json")[1]
        inp = inp.model_copy(update={"busy": (span(5, 15, 16),)})
        prev = list(pack(inp).blocks)
        moved = inp.model_copy(update={"busy": (span(1, 7, 10, 0, 30),)})
        return "PRD §6: a calendar event moves onto Tuesday's session; lineage kept.", moved, prev

    def pause() -> Seed:
        inp, prev = advance(baseline(), t(1, 12))
        inp = inp.model_copy(
            update={"projects": tuple(p for p in inp.projects if p.id != "reading")}
        )
        return "PRD §6 'pause project X': reading removed mid-week.", inp, prev

    def double_gym() -> Seed:
        inp, prev = advance(baseline(), t(2, 8))
        inp = inp.model_copy(
            update={
                "projects": tuple(
                    p.model_copy(update={"weekly_target_minutes": 480}) if p.id == "workout" else p
                    for p in inp.projects
                )
            }
        )
        return "PRD §6 'double my gym time': workout 240 -> 480 min Wednesday morning.", inp, prev

    def mornings_gone() -> Seed:
        forbids = [
            {"id": f"no-am-{d}", "kind": "forbid", "span": span(d, 8, 12).model_dump(mode="json")}
            for d in range(2, 7)
        ]
        inp, prev = advance(baseline(), t(1, 18), pins=forbids)
        return "PRD §6 'my mornings are gone': blanket forbids Wed-Sun 08-12.", inp, prev

    def finished_early() -> Seed:
        inp, prev = advance(baseline(), t(3, 9))
        inp = inp.model_copy(
            update={
                "projects": tuple(
                    p.model_copy(update={"remaining_minutes": 60}) if p.id == "portfolio" else p
                    for p in inp.projects
                )
            }
        )
        return "PRD §6 'I finished the deck early': remaining drops to 60 min.", inp, prev

    def manual_edit() -> Seed:
        inp, prev = advance(baseline(), t(1, 7))
        portfolio = [b for b in prev if b.project_id == "portfolio"]
        dragged, deleted = portfolio[0], portfolio[1]
        pins = [
            Pin(
                id="pin-drag",
                kind="pin",
                span=Interval(
                    start=dragged.span.start + timedelta(hours=3),
                    end=dragged.span.end + timedelta(hours=3, minutes=30),
                ),
                project_id="portfolio",
                created_from_lineage_id=dragged.lineage_id,
            ),
            Pin(id="forbid-del", kind="forbid", span=deleted.span, project_id="portfolio"),
        ]
        inp = inp.model_copy(update={"pins": tuple(pins)})
        return (
            "PRD §5 step 3 / §6: a dragged+resized block becomes a pin (keeps its lineage); "
            "a deleted block becomes a project forbid.",
            inp,
            prev,
        )

    return {
        "resched_no_laptop_tuesday": no_laptop,
        "resched_calendar_moved": calendar_moved,
        "resched_pause_project": pause,
        "resched_double_gym": double_gym,
        "resched_mornings_gone": mornings_gone,
        "resched_finished_early": finished_early,
        "resched_manual_edit_pin": manual_edit,
    }


def _infeasible_seeds() -> dict[str, Callable[[], Seed]]:
    sun = date(2026, 11, 22)

    def zero_free() -> Seed:
        inp = solver_input(
            busy=week(7, 21),
            projects=[deadline_project("thesis", 600, sun), perennial("gym", 120)],
        )
        return "Infeasible: every waking hour is busy (zero free time).", inp, []

    def short_horizon() -> Seed:
        inp = solver_input(
            horizon=Interval(start=MONDAY, end=t(1, 0)),
            projects=[
                deadline_project("grant", 900, date(2026, 11, 16), max_block_minutes=180),
                perennial("walk", 120),
            ],
        )
        return "Infeasible: a one-day horizon shorter than the demand due today.", inp, []

    def conflicting_pins() -> Seed:
        pins = [
            Pin(id="p-a", kind="pin", span=span(0, 9, 11), project_id="book"),
            Pin(id="p-overlap", kind="pin", span=span(0, 10, 12), project_id="code"),
            Pin(id="p-busy", kind="pin", span=span(1, 13, 15), project_id="book"),
            Pin(id="p-exc", kind="pin", span=span(2, 9, 11), project_id="code"),
            Pin(id="p-forbid", kind="pin", span=span(3, 9, 10), project_id="book"),
            Pin(id="f-thu", kind="forbid", span=span(3, 8, 20)),
            Pin(id="p-ok", kind="pin", span=span(4, 14, 16), project_id="code"),
        ]
        inp = solver_input(
            busy=[span(1, 14, 15), *(span(d, 12, 19) for d in range(5))],
            exceptions=[
                ExceptionInput(
                    id="no-laptop", span=Interval(start=t(2, 0), end=t(3, 0)), tags=("laptop",)
                ),
            ],
            pins=pins,
            projects=[
                deadline_project("book", 1500, sun),
                deadline_project("code", 1200, sun, requirement_tags=("laptop",)),
            ],
        )
        return (
            "Infeasible: overlapping pins, pins on busy/exception/forbid, and demand above "
            "capacity -> PinInvalidated + DemandShortfall + relaxations.",
            inp,
            [],
        )

    return {
        "infeasible_zero_free_time": zero_free,
        "infeasible_short_horizon": short_horizon,
        "infeasible_conflicting_pins": conflicting_pins,
    }


def seeds() -> dict[str, Callable[[], Seed]]:
    return {
        **dict(_fixture_seeds()),
        **_rule_seeds(),
        **_resched_seeds(),
        **_infeasible_seeds(),
    }


def render_input(description: str, inp: SolverInput, previous: list[PlannedBlock]) -> str:
    envelope = {
        "description": description,
        "solver_input": inp.model_dump(mode="json"),
        "previous": [b.model_dump(mode="json") for b in previous],
    }
    return json.dumps(envelope, indent=2) + "\n"


def seed_inputs(only: list[str]) -> int:
    builders = seeds()
    unknown = sorted(set(only) - set(builders))
    if unknown:
        print(f"unknown seeds: {', '.join(unknown)}", file=sys.stderr)
        return 2
    refused = 0
    for name in only or sorted(builders):
        path = input_path(name)
        if path.exists():
            continue
        description, inp, previous = builders[name]()
        out = pack(inp, previous)
        problems = verify(out.blocks, inp)
        if problems:
            refused += 1
            kinds = sorted({v.kind for v in problems})
            print(f"REFUSED {name}: pack() output fails verify(): {kinds}", file=sys.stderr)
            continue
        path.write_text(render_input(description, inp, previous))
        print(f"seeded: {name}")
    return 1 if refused else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", default=[], metavar="NAME")
    parser.add_argument("--seed-inputs", action="store_true")
    args = parser.parse_args(argv)
    if args.seed_inputs:
        status = seed_inputs(args.only)
        if status == 2:
            return status
        return regenerate([n for n in args.only if n in corpus_names()]) or status
    return regenerate(args.only)


if __name__ == "__main__":
    raise SystemExit(main())

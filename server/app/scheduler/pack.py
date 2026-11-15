"""Greedy, scored packer (ARCHITECTURE.md §4; PRD §5 step 2, §6).

Pure and deterministic: no clock, environment, entropy source, or timezone database. All
geometry is integer minutes from `horizon.start`; every iterable that feeds an ordering is
sorted on a total key first, so the plan does not depend on input list order. `now` is
rounded UP to the next whole minute and then to the 5-minute grid, so no block starts
before the real instant.

Pipeline:
1. Frozen blocks are occupied time and keep their lineages reserved. They are not repeated
   in `blocks`: verify() treats `blocks` as the planned remainder, and a frozen block there
   would be a duplicate lineage overlapping itself (and `before_now`). Copy-forward of past
   rows verbatim happens at confirm (ARCH §3 Semantics).
2. `pin`-kind Pins are placed exactly on their span with `pinned=True`, in (start, end, id)
   order. Each candidate is checked by verify() against the static constraints and only the
   already-placed blocks within buffer reach (never a re-verify of the growing prefix); a
   rejected pin is not placed and is reported as one `PinInvalidated` per conflict
   (calendar > exceptions > pins). verify() does not count an unrealizable pin as
   `pin_missing`, so the invariant verify(blocks, input) == [] holds for every plan.
3. Demand: perennial = weekly target minus frozen non-skipped minutes in the horizon;
   deadline = `remaining_minutes` minus frozen `planned` (in-progress) minutes in the horizon,
   clamped at 0, divided over the horizon-length periods left until `deadline`. A deadline
   project is placed only before min(horizon.end, deadline).
4. Remaining demand is packed one block at a time: pick the project under the most pressure
   (demand left / free minutes left), generate snapped candidates in its free time, score
   them with the weights below, place the best. A project with no candidate is stuck.
5. Lineages: pins keep `created_from_lineage_id`; other blocks reuse an unmatched previous
   block's lineage (same project, overlapping or within 24h, nearest start first), else mint
   uuid5("{project}:{ordinal}:{salt}") with the salt raised until the id differs from every
   frozen, pin, previous (matched or not), and already-minted lineage.
6. Every unmet demand is a `DemandShortfall`. Every candidate relaxation is priced exactly by
   re-packing the input with the mutation applied (recovers = drop in total shortfall), and
   the work is bounded: remove_pin only for the MAX_PIN_COUNTERFACTUALS pins that overlap the
   short projects' allowed time most, one relaxation per kind per short project, and
   extend_deadline only up to EXTEND_DEADLINE_CAP_DAYS after horizon.start. No date or period
   computation can raise for contract-valid inputs, so the partial plan is always returned.
"""

from __future__ import annotations

import uuid
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .models import (
    DemandShortfall,
    ExceptionInput,
    FrozenBlock,
    Infeasibility,
    InfeasibilityViolation,
    Interval,
    Pin,
    PinConflict,
    PinInvalidated,
    PlannedBlock,
    ProjectInput,
    Relaxation,
    SolverInput,
    SolverOutput,
    Violation,
)
from .verify import verify

SOLVER_VERSION = "1.0.0"

# ------------------------------------------------------------------ scoring weights
# Candidate score = sum of the terms below; the highest score wins, ties go to the earliest
# start, then the shortest block. Integer points throughout.
W_MINUTES = 1  # per placed minute: prefer full-length sittings over fragments
W_SPREAD_SAME_DAY = 150  # per existing block of the project that day (spacing, PRD §5.2)
W_BACK_TO_BACK = 100  # project already within BACK_TO_BACK_GAP of the candidate
W_EARLY_PER_DAY = 5  # deadline projects: per day of delay, so slack stays late in the week
W_PREFERRED = 200  # candidate inside a non-strict preferred window
W_RHYTHM_PER_30 = 10  # perennial: per 30 min of time-of-day drift from its other blocks
RHYTHM_CAP = 120  # max rhythm penalty, so rhythm never beats spacing
W_MIXED_VARIETY = 40  # mixed: per distinct other project already that day (capped at 3)
W_FOCUSED_SAME_DAY = 200  # focused: project already has a block that day (themed day)
W_FOCUSED_OTHER = 120  # focused: per distinct other project already that day
W_OVER_SLACK = 100_000  # day past SLACK_PERCENT of its free time: only when nothing else fits
W_STABLE_SAME_SPAN = 400  # candidate equals a previous block of the project (stable re-solve)
W_STABLE_OVERLAP = 100  # candidate overlaps a previous block of the project
# raise_max_block_minutes proposes the first of 1.5x, 2x, longest free run that recovers time.

# Relaxation work bounds (bounded so one pack() stays interactive with many pins).
MAX_PIN_COUNTERFACTUALS = 8  # remove_pin: re-packs considered, ranked by overlap
EXTEND_DEADLINE_CAP_DAYS = 730  # extend_deadline never proposes past horizon.start + this

SLACK_PERCENT = 85
BACK_TO_BACK_GAP = 60  # minutes
CANDIDATE_STEP = 30  # minutes between candidate starts inside a free segment
GRID = 5  # block boundaries snap to absolute 5-minute multiples
DAY = 24 * 60
LINEAGE_REUSE_WINDOW = DAY  # a previous block within 24h may lend its lineage
PRESSURE_SCALE = 1_000_000

LINEAGE_NAMESPACE = uuid.UUID("6f1d3c2a-4b5e-5f60-8a7b-9c0d1e2f3a4b")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MINUTE = timedelta(minutes=1)

# Conflict order for reporting an invalidated pin (ARCH §3: calendar > exceptions > pins).
_CONFLICT_ORDER: tuple[PinConflict, ...] = (
    "before_now",
    "horizon",
    "window",
    "busy",
    "exception",
    "forbid",
    "preferred_window",
    "overlap",
    "buffer",
)

Span = tuple[int, int]


# --------------------------------------------------------------- interval algebra


def _merge(spans: Iterable[Span]) -> list[Span]:
    out: list[Span] = []
    for s, e in sorted(spans):
        if s >= e:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(base: list[Span], cut: list[Span]) -> list[Span]:
    """base minus cut; both sorted and disjoint."""
    out: list[Span] = []
    j = 0
    for s, e in base:
        cur = s
        while j < len(cut) and cut[j][1] <= cur:
            j += 1
        k = j
        while k < len(cut) and cut[k][0] < e:
            cs, ce = cut[k]
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
            if cur >= e:
                break
            k += 1
        if cur < e:
            out.append((cur, e))
    return out


def _intersect(a: list[Span], b: list[Span]) -> list[Span]:
    out: list[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _total(spans: Iterable[Span]) -> int:
    return sum(e - s for s, e in spans)


def _inside(span: Span, spans: list[Span]) -> bool:
    return any(s <= span[0] and span[1] <= e for s, e in spans)


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# ------------------------------------------------------------------- the problem


@dataclass
class _Placed:
    project_id: str
    span: Span
    pinned: bool
    lineage_id: str | None = None  # pins: decided at placement; others: at lineage matching
    pin_ids: tuple[str, ...] = ()


@dataclass
class _Demand:
    project: ProjectInput
    required: int
    left: int
    lengths: tuple[int, int] | None  # snapped (min, max) block length, None if none fits
    base: list[Span]  # static allowed time (before occupancy)
    on_time_end: int  # minute after which placed time does not count toward demand (deadline)
    stuck: bool = False

    def on_time(self, span: Span) -> int:
        """Minutes of `span` that count toward this demand (before the deadline bound)."""
        return max(0, min(span[1], self.on_time_end) - span[0])


@dataclass
class _Result:
    blocks: list[_Placed]
    demands: dict[str, _Demand]
    invalidated: list[PinInvalidated]
    trial_violations: list[Violation]
    rejected_pin_ids: set[str] = field(default_factory=set)

    def shortfall(self, project_id: str) -> int:
        d = self.demands.get(project_id)
        return max(0, d.required - self.scheduled(project_id)) if d else 0

    def scheduled(self, project_id: str) -> int:
        # Only on-time minutes count: a pin the user placed after a deadline is kept
        # (pins are user overrides, O6) but never satisfies deadline demand.
        d = self.demands.get(project_id)
        return sum(
            d.on_time(b.span) if d else b.span[1] - b.span[0]
            for b in self.blocks
            if b.project_id == project_id
        )


class _Clock:
    """Minute-grid conversion relative to horizon.start."""

    def __init__(self, inp: SolverInput) -> None:
        self.origin = inp.horizon.start
        self.horizon = (0, self.to_min(inp.horizon.end))
        # Grid points g satisfy (absolute minute of origin + g) % GRID == 0.
        self.g0 = (-((self.origin - _EPOCH) // _MINUTE)) % GRID
        # First allowed start: `now` rounded UP to a whole minute, then up to the grid.
        self.now = max(0, self.up(-((self.origin - inp.now) // _MINUTE)))

    def to_min(self, dt: datetime) -> int:
        return (dt - self.origin) // _MINUTE

    def span(self, iv: Interval) -> Span:
        return (self.to_min(iv.start), self.to_min(iv.end))

    def to_dt(self, m: int) -> datetime:
        return self.origin + timedelta(minutes=m)

    def interval(self, span: Span) -> Interval:
        return Interval(start=self.to_dt(span[0]), end=self.to_dt(span[1]))

    def up(self, m: int) -> int:
        return m + ((self.g0 - m) % GRID)

    def down(self, m: int) -> int:
        return m - ((m - self.g0) % GRID)


def _pin_key(p: Pin) -> tuple[datetime, datetime, str, str, str, str]:
    return (
        p.span.start,
        p.span.end,
        p.id,
        p.kind,
        p.project_id or "",
        p.created_from_lineage_id or "",
    )


def _exc_key(e: ExceptionInput) -> tuple[str, datetime, datetime]:
    return (e.id, e.span.start, e.span.end)


def _frozen_key(f: FrozenBlock) -> tuple[datetime, datetime, str, str, str]:
    return (f.span.start, f.span.end, f.lineage_id, f.project_id, f.state)


def _canonical(inp: SolverInput) -> SolverInput:
    """Same constraints, every list in a total order."""
    return inp.model_copy(
        update={
            "busy": tuple(sorted(inp.busy, key=lambda i: (i.start, i.end))),
            "schedulable": tuple(sorted(inp.schedulable, key=lambda i: (i.start, i.end))),
            "frozen": tuple(sorted(inp.frozen, key=_frozen_key)),
            "pins": tuple(sorted(inp.pins, key=_pin_key)),
            "projects": tuple(sorted(inp.projects, key=lambda p: p.id)),
            "exceptions": tuple(sorted(inp.exceptions, key=_exc_key)),
        }
    )


def _blocks_project(e: ExceptionInput, p: ProjectInput) -> bool:
    return e.blocks_all or bool(set(p.requirement_tags).intersection(e.tags))


def _forbids_project(pin: Pin, project_id: str) -> bool:
    return pin.kind == "forbid" and (pin.project_id is None or pin.project_id == project_id)


def _uuid(name: str) -> str:
    return str(uuid.uuid5(LINEAGE_NAMESPACE, name))


def _frozen_minutes(p: ProjectInput, inp: SolverInput, clk: _Clock, states: set[str]) -> int:
    """Minutes of the project's frozen blocks in `states`, clipped to the horizon."""
    return sum(
        max(0, min(e, clk.horizon[1]) - max(s, 0))
        for f in inp.frozen
        if f.project_id == p.id and f.state in states
        for s, e in [clk.span(f.span)]
    )


def _inflight_minutes(p: ProjectInput, inp: SolverInput, clk: _Clock) -> int:
    """Frozen `planned` (in-progress) minutes: not yet subtracted from remaining_minutes."""
    return _frozen_minutes(p, inp, clk, {"planned"})


def _required_minutes(p: ProjectInput, inp: SolverInput, clk: _Clock) -> int:
    """Demand inside this horizon.

    Perennial (`weekly_target_minutes` set): the target minus frozen minutes already spent in
    the horizon (skipped sessions do not count). Deadline: `remaining_minutes` minus frozen
    in-progress minutes (completed work is already excluded from it), clamped at 0, divided
    over the horizon-length periods left until `deadline`.
    """
    if p.weekly_target_minutes is not None:
        done = _frozen_minutes(p, inp, clk, {"planned", "completed"})
        return max(0, p.weekly_target_minutes - done)
    demand = max(0, p.remaining_minutes - _inflight_minutes(p, inp, clk))
    return _ceil_div(demand, _periods_left(p, clk))


def _periods_left(p: ProjectInput, clk: _Clock) -> int:
    """Horizon-length periods from horizon.start until `deadline` (at least 1)."""
    if p.deadline is None:
        return 1
    return max(1, _ceil_div(clk.to_min(p.deadline), clk.horizon[1]))


def _placement_end(p: ProjectInput, clk: _Clock) -> int:
    """Deadline projects are placed only before min(horizon.end, deadline)."""
    if p.weekly_target_minutes is not None or p.deadline is None:
        return clk.horizon[1]
    return max(0, min(clk.horizon[1], clk.to_min(p.deadline)))


# ------------------------------------------------------------------------ solving


def _place_pins(
    inp: SolverInput, clk: _Clock
) -> tuple[list[_Placed], list[PinInvalidated], list[Violation], set[str], set[str]]:
    """Place pin-kind pins that verify() accepts; report the rest.

    Each new pin is verified against the static constraints plus only the already-placed
    blocks (and frozen blocks) within buffer reach of it: the only ones that can conflict.
    """
    forbids = [p for p in inp.pins if p.kind == "forbid"]
    used_lineages = {f.lineage_id for f in inp.frozen}
    buffer = inp.buffer_minutes
    placed: list[_Placed] = []
    placed_blocks: list[PlannedBlock] = []
    placed_pins: dict[str, list[Pin]] = {}  # lineage -> pins it realizes
    by_span: dict[tuple[str, Span], _Placed] = {}
    invalidated: list[PinInvalidated] = []
    trial: list[Violation] = []
    rejected: set[str] = set()

    def near(span: Interval) -> bool:
        return span.start - timedelta(
            minutes=buffer
        ) < cand_span.end and cand_span.start < span.end + timedelta(minutes=buffer)

    for pin in inp.pins:
        if pin.kind != "pin" or pin.project_id is None:
            continue
        twin = by_span.get((pin.project_id, clk.span(pin.span)))
        if twin is not None:
            twin.pin_ids = (*twin.pin_ids, pin.id)
            placed_pins[twin.lineage_id or ""].append(pin)
            continue
        if any(f.project_id == pin.project_id and f.span == pin.span for f in inp.frozen):
            continue  # already realized by the represented past
        lineage = pin.created_from_lineage_id
        if lineage is None or lineage in used_lineages:
            lineage = _uuid(f"pin:{pin.id}")
            n = 0
            while lineage in used_lineages:
                n += 1
                lineage = _uuid(f"pin:{pin.id}:{n}")
        candidate = PlannedBlock(
            lineage_id=lineage, project_id=pin.project_id, span=pin.span, pinned=True
        )
        cand_span = pin.span
        neighbours = [b for b in placed_blocks if near(b.span)]
        trial_input = inp.model_copy(
            update={
                "pins": (
                    *(f for f in forbids if f.span.overlaps(cand_span)),
                    *(q for b in neighbours for q in placed_pins[b.lineage_id]),
                    pin,
                ),
                "frozen": tuple(f for f in inp.frozen if near(f.span)),
            }
        )
        hits = [
            v
            for v in verify([*neighbours, candidate], trial_input)
            if lineage in v.block_lineage_ids
        ]
        if not hits:
            used_lineages.add(lineage)
            block = _Placed(pin.project_id, clk.span(pin.span), True, lineage, (pin.id,))
            placed.append(block)
            by_span[(pin.project_id, block.span)] = block
            placed_blocks.append(candidate)
            placed_pins[lineage] = [pin]
            continue
        rejected.add(pin.id)
        trial.extend(hits)
        exc_ids = {e.id for e in inp.exceptions}
        reports: dict[tuple[str, str | None], PinInvalidated] = {}
        for v in hits:
            if v.kind not in _CONFLICT_ORDER:
                continue  # unknown_project: kept in verify_violations only
            kind: PinConflict = v.kind  # type: ignore[assignment]
            ex_ids = [i for i in v.related_ids if i in exc_ids] if kind == "exception" else []
            for ex in ex_ids or [None]:
                others = tuple(i for i in v.block_lineage_ids if i != lineage)
                key = (kind, ex)
                prev = reports.get(key)
                if prev is not None:
                    others = (*prev.related_lineage_ids, *others)
                reports[key] = PinInvalidated(
                    pin_id=pin.id,
                    pin_kind=pin.kind,
                    project_id=pin.project_id,
                    span=pin.span,
                    conflicts_with=kind,
                    exception_id=ex,
                    related_lineage_ids=others,
                )
        invalidated.extend(
            reports[k]
            for k in sorted(reports, key=lambda k: (_CONFLICT_ORDER.index(k[0]), k[1] or ""))
        )
    return placed, invalidated, trial, rejected, used_lineages


def _base_allowed(
    p: ProjectInput, inp: SolverInput, clk: _Clock, *, removable: bool = True
) -> list[Span]:
    """Static allowed time; `removable=False` keeps exceptions and forbids out of the cut."""
    allowed = _intersect(
        _merge(clk.span(i) for i in inp.schedulable), [(clk.now, _placement_end(p, clk))]
    )
    blocked = [clk.span(i) for i in inp.busy]
    if removable:
        blocked += [clk.span(e.span) for e in inp.exceptions if _blocks_project(e, p)]
        blocked += [clk.span(x.span) for x in inp.pins if _forbids_project(x, p.id)]
    allowed = _subtract(allowed, _merge(blocked))
    if p.preferred_windows_strict:
        allowed = _intersect(allowed, _merge(clk.span(w) for w in p.preferred_windows))
    return allowed


def _lengths(p: ProjectInput) -> tuple[int, int] | None:
    lo = _ceil_div(p.min_block_minutes, GRID) * GRID
    hi = p.max_block_minutes // GRID * GRID
    return (lo, hi) if lo <= hi else None


@dataclass
class _Context:
    inp: SolverInput
    clk: _Clock
    occupied: list[_Placed]  # frozen + placed, for day composition
    frozen_spans: list[Span]
    day_capacity: dict[int, int]
    previous: dict[str, list[Span]]
    preferred: dict[str, list[Span]]


def _day(m: int) -> int:
    return m // DAY


@dataclass
class _Snapshot:
    """Day composition at one placement step, computed once for all candidates."""

    own_per_day: dict[int, int]  # this project's blocks per day
    others_per_day: dict[int, int]  # distinct other projects per day
    used_per_day: dict[int, int]  # occupied minutes per day
    own_starts: list[int]  # this project's block starts, sorted
    own_end_max: list[int]  # own_end_max[i] = max end of the first i+1 blocks by start
    tods: list[int]


def _snapshot(ctx: _Context, project_id: str) -> _Snapshot:
    own_per_day: dict[int, int] = {}
    others: dict[int, set[str]] = {}
    used: dict[int, int] = {}
    own: list[Span] = []
    for b in ctx.occupied:
        s, e = b.span
        day = _day(s)
        if b.project_id == project_id:
            own_per_day[day] = own_per_day.get(day, 0) + 1
            own.append(b.span)
        else:
            others.setdefault(day, set()).add(b.project_id)
        for d in range(_day(s), _day(e - 1) + 1):
            used[d] = used.get(d, 0) + min(e, (d + 1) * DAY) - max(s, d * DAY)
    return _Snapshot(
        own_per_day=own_per_day,
        others_per_day={d: len(v) for d, v in others.items()},
        used_per_day=used,
        own_starts=[s for s, _ in sorted(own)],
        own_end_max=_prefix_max([e for _, e in sorted(own)]),
        tods=sorted({s % DAY for s, _ in own}),
    )


def _prefix_max(values: list[int]) -> list[int]:
    out: list[int] = []
    for v in values:
        out.append(max(v, out[-1]) if out else v)
    return out


def _near_own(snap: _Snapshot, s: int, e: int) -> bool:
    """Some own block lies within BACK_TO_BACK_GAP of [s, e)."""
    i = bisect_left(snap.own_starts, e + BACK_TO_BACK_GAP)  # blocks starting before e + gap
    return i > 0 and snap.own_end_max[i - 1] > s - BACK_TO_BACK_GAP


def _drift(tods: list[int], tod: int) -> int:
    """Circular distance from `tod` to the nearest time of day in sorted `tods`."""
    i = bisect_left(tods, tod)
    near = [tods[i % len(tods)], tods[i - 1]]
    return min(min(abs(tod - t), DAY - abs(tod - t)) for t in near)


def _score(ctx: _Context, snap: _Snapshot, d: _Demand, span: Span) -> int:
    p = d.project
    s, e = span
    day = _day(s)
    same_day = snap.own_per_day.get(day, 0)
    others = snap.others_per_day.get(day, 0)
    perennial = p.weekly_target_minutes is not None

    score = W_MINUTES * (e - s)
    if ctx.inp.day_diversity == "focused":
        # Themed days: join the project's day, avoid days owned by other projects.
        score += W_FOCUSED_SAME_DAY if same_day else 0
        score -= W_FOCUSED_OTHER * others
    else:
        score -= W_SPREAD_SAME_DAY * same_day
        score += W_MIXED_VARIETY * min(3, others)
    if _near_own(snap, s, e):
        score -= W_BACK_TO_BACK
    if not perennial:
        score -= W_EARLY_PER_DAY * day
    if not p.preferred_windows_strict and _inside(span, ctx.preferred[p.id]):
        score += W_PREFERRED
    if perennial and snap.tods:
        tod = s % DAY
        drift = _drift(snap.tods, tod)
        score -= min(RHYTHM_CAP, W_RHYTHM_PER_30 * (drift // 30))
    if (snap.used_per_day.get(day, 0) + e - s) * 100 > ctx.day_capacity.get(day, 0) * SLACK_PERCENT:
        score -= W_OVER_SLACK
    prev = ctx.previous.get(p.id, [])
    if span in prev:
        score += W_STABLE_SAME_SPAN
    elif any(ps < e and s < pe for ps, pe in prev):
        score += W_STABLE_OVERLAP
    return score


def _candidates(ctx: _Context, d: _Demand, free: list[Span]) -> list[Span]:
    assert d.lengths is not None
    lo, hi = d.lengths
    want = min(hi, max(lo, _ceil_div(d.left, GRID) * GRID))
    clk = ctx.clk
    anchors = sorted(
        {b.span[0] % DAY for b in ctx.occupied if b.project_id == d.project.id}
        | {ps % DAY for ps, _ in ctx.previous.get(d.project.id, [])}
    )
    out: set[Span] = set()
    for a, b in free:
        s0, e0 = clk.up(a), clk.down(b)
        if e0 - s0 < lo:
            continue
        length = min(want, e0 - s0)
        last = e0 - length
        starts = set(range(s0, last + 1, CANDIDATE_STEP)) | {last}
        for day in range(_day(s0), _day(last) + 1):
            for t in anchors:
                x = clk.up(day * DAY + t)
                if s0 <= x <= last:
                    starts.add(x)
        for ps, pe in ctx.previous.get(d.project.id, []):
            aligned = clk.up(ps) == ps and clk.up(pe) == pe
            if aligned and s0 <= ps and pe <= e0 and lo <= pe - ps <= hi:
                out.add((ps, pe))
        out.update((x, x + length) for x in starts)
    return sorted(out)


def _solve(inp: SolverInput, previous: Sequence[PlannedBlock]) -> _Result:
    clk = _Clock(inp)
    pins_placed, invalidated, trial, rejected, used = _place_pins(inp, clk)

    frozen_spans = [clk.span(f.span) for f in inp.frozen]
    occupied = [_Placed(f.project_id, clk.span(f.span), False, f.lineage_id) for f in inp.frozen]
    occupied += pins_placed
    buffer = inp.buffer_minutes

    # Day capacity for the slack rule: schedulable, after now, minus busy.
    start = clk.now
    open_time = _subtract(
        _intersect(_merge(clk.span(i) for i in inp.schedulable), [(start, clk.horizon[1])]),
        _merge(clk.span(i) for i in inp.busy),
    )
    capacity: dict[int, int] = {}
    for s, e in open_time:
        for day in range(_day(s), _day(e - 1) + 1):
            capacity[day] = capacity.get(day, 0) + min(e, (day + 1) * DAY) - max(s, day * DAY)
    for f in frozen_spans:  # frozen time in the future counts toward the day's free time
        for day in range(_day(f[0]), _day(f[1] - 1) + 1):
            if f[1] > start:
                capacity[day] = capacity.get(day, 0) + max(
                    0, min(f[1], (day + 1) * DAY) - max(f[0], day * DAY, start)
                )

    prev_by_project: dict[str, list[Span]] = {}
    for b in sorted(previous, key=lambda b: (b.span.start, b.span.end, b.lineage_id)):
        prev_by_project.setdefault(b.project_id, []).append(clk.span(b.span))

    ctx = _Context(
        inp=inp,
        clk=clk,
        occupied=occupied,
        frozen_spans=frozen_spans,
        day_capacity=capacity,
        previous=prev_by_project,
        preferred={p.id: _merge(clk.span(w) for w in p.preferred_windows) for p in inp.projects},
    )

    demands: dict[str, _Demand] = {}
    for p in inp.projects:
        required = _required_minutes(p, inp, clk)
        on_time_end = _placement_end(p, clk)
        pinned = sum(
            max(0, min(b.span[1], on_time_end) - b.span[0])
            for b in pins_placed
            if b.project_id == p.id
        )
        demands[p.id] = _Demand(
            project=p,
            required=required,
            left=max(0, required - pinned),
            lengths=_lengths(p),
            base=_base_allowed(p, inp, clk),
            on_time_end=on_time_end,
        )

    while True:
        expanded = _merge((s - buffer, e + buffer) for s, e in (b.span for b in occupied))
        best: tuple[int, str, list[Span]] | None = None
        for pid in sorted(demands):
            d = demands[pid]
            if d.left <= 0 or d.stuck or d.lengths is None:
                continue
            free = _subtract(d.base, expanded)
            room = _total(free)
            if room < d.lengths[0]:
                d.stuck = True
                continue
            pressure = d.left * PRESSURE_SCALE // room
            if best is None or pressure > best[0]:
                best = (pressure, pid, free)
        if best is None:
            break
        _, pid, free = best
        d = demands[pid]
        cands = _candidates(ctx, d, free)
        if not cands:
            d.stuck = True
            continue
        snap = _snapshot(ctx, pid)
        span = min(cands, key=lambda c: (-_score(ctx, snap, d, c), c[0], c[1] - c[0]))
        occupied.append(_Placed(pid, span, False))
        d.left -= span[1] - span[0]

    blocks = [b for b in occupied if b.pinned or b.lineage_id is None]
    _assign_lineages(blocks, previous, clk, used)
    blocks.sort(key=lambda b: (b.span, b.project_id, b.lineage_id or ""))
    return _Result(blocks, demands, invalidated, trial, rejected)


def _assign_lineages(
    blocks: list[_Placed], previous: Sequence[PlannedBlock], clk: _Clock, used: set[str]
) -> None:
    """Greedy stable matching: nearest start first, each previous lineage at most once."""
    prev = [
        (clk.span(p.span), p.project_id, p.lineage_id) for p in previous if p.lineage_id not in used
    ]
    open_blocks = [b for b in blocks if b.lineage_id is None]
    pairs: list[tuple[int, Span, str, str, int]] = []
    for i, b in enumerate(open_blocks):
        for (ps, pe), pid, lid in prev:
            if pid != b.project_id:
                continue
            gap = max(0, max(ps, b.span[0]) - min(pe, b.span[1]))
            if gap <= LINEAGE_REUSE_WINDOW:
                pairs.append((abs(ps - b.span[0]), b.span, lid, b.project_id, i))
    pairs.sort()
    taken: set[str] = set()
    for _, _, lid, _, i in pairs:
        b = open_blocks[i]
        if b.lineage_id is None and lid not in taken and lid not in used:
            b.lineage_id = lid
            taken.add(lid)
    # Minted ids avoid every frozen, pin, and previous lineage (matched or not) and each other.
    used |= taken
    used |= {p.lineage_id for p in previous}
    ordinals: dict[str, int] = {}
    for b in sorted(open_blocks, key=lambda b: (b.span, b.project_id)):
        if b.lineage_id is not None:
            continue
        n = ordinals.get(b.project_id, 0)
        ordinals[b.project_id] = n + 1
        salt = 0
        lid = _uuid(f"{b.project_id}:{n}:{salt}")
        while lid in used:
            salt += 1
            lid = _uuid(f"{b.project_id}:{n}:{salt}")
        b.lineage_id = lid
        used.add(lid)


# --------------------------------------------------------------- infeasibility


def _total_shortfall(r: _Result, exclude: str | None = None) -> dict[str, int]:
    return {pid: r.shortfall(pid) for pid in r.demands if pid != exclude}


def _gain(before: _Result, after: _Result, exclude: str | None = None) -> tuple[int, list[str]]:
    b = _total_shortfall(before, exclude)
    a = _total_shortfall(after, exclude)
    gain = sum(b.values()) - sum(a.get(pid, 0) for pid in b)
    helped = sorted(pid for pid in b if a.get(pid, 0) < b[pid])
    return gain, helped


def _overlap(span: Span, spans: list[Span]) -> int:
    return sum(max(0, min(span[1], e) - max(span[0], s)) for s, e in spans)


def _shift_deadline(
    p: ProjectInput, inp: SolverInput, clk: _Clock, until: int
) -> tuple[date, datetime] | None:
    """Move target_date and deadline by the fewest whole days (at least one) that put the
    deadline at or after `until` minutes from horizon.start. None past the cap, or when the
    result is not representable; integer checks run before any date is built."""
    if p.target_date is None or p.deadline is None:
        return None
    shift = max(1, _ceil_div(until - clk.to_min(p.deadline), DAY))
    if p.target_date.toordinal() + shift > (
        inp.horizon.start.date().toordinal() + EXTEND_DEADLINE_CAP_DAYS
    ):
        return None
    try:
        return date.fromordinal(p.target_date.toordinal() + shift), p.deadline + timedelta(
            days=shift
        )
    except (OverflowError, ValueError):
        return None


def _relaxations(
    inp: SolverInput, previous: Sequence[PlannedBlock], base: _Result
) -> list[Relaxation]:
    """Candidate trade-offs, each priced by re-packing the input with it applied."""
    clk = _Clock(inp)
    short = {pid: s for pid in sorted(base.demands) if (s := base.shortfall(pid)) > 0}
    out: list[Relaxation] = []

    def counterfactual(**update: object) -> _Result:
        return _solve(_canonical(inp.model_copy(update=update)), previous)

    def with_project(pid: str, **update: object) -> tuple[ProjectInput, ...]:
        return tuple(q.model_copy(update=update) if q.id == pid else q for q in inp.projects)

    def offer(result: _Result, exclude: str | None = None, **fields: object) -> bool:
        gain, helped = _gain(base, result, exclude)
        if gain <= 0:
            return False
        out.append(
            Relaxation(affected_ids=tuple(helped), recovers_minutes=gain, **fields)  # type: ignore[arg-type]
        )
        return True

    # Demand side: one per kind per short project.
    for pid in short:
        p = base.demands[pid].project
        scheduled = base.scheduled(pid)
        if p.weekly_target_minutes is not None:
            target = p.weekly_target_minutes - short[pid]
            if target > 0:
                offer(
                    counterfactual(projects=with_project(pid, weekly_target_minutes=target)),
                    kind="reduce_weekly_target",
                    description=f"Lower {pid}'s weekly target to {target} min",
                    project_id=pid,
                    weekly_target_minutes=target,
                )
            continue
        remaining = scheduled * _periods_left(p, clk) + _inflight_minutes(p, inp, clk)
        if 0 < remaining < p.remaining_minutes:
            offer(
                counterfactual(projects=with_project(pid, remaining_minutes=remaining)),
                kind="reduce_estimate",
                description=f"Lower {pid}'s remaining estimate to {remaining} min",
                project_id=pid,
                remaining_minutes=remaining,
            )
        per_period, until = scheduled, 0
        if p.deadline is not None and clk.to_min(p.deadline) < clk.horizon[1]:
            # The deadline cuts this horizon: see where the work lands with the horizon open.
            full = _shift_deadline(p, inp, clk, clk.horizon[1])
            if full is not None:
                trial = counterfactual(projects=with_project(pid, deadline=full[1]))
                per_period = trial.scheduled(pid)
                if trial.shortfall(pid) == 0:
                    until = max((x.span[1] for x in trial.blocks if x.project_id == pid), default=0)
        if per_period > 0:
            demand = max(0, p.remaining_minutes - _inflight_minutes(p, inp, clk))
            until = until or _ceil_div(demand, per_period) * clk.horizon[1]
            moved = _shift_deadline(p, inp, clk, until)
            if moved is not None:
                new_date, new_deadline = moved
                offer(
                    counterfactual(
                        projects=with_project(pid, target_date=new_date, deadline=new_deadline)
                    ),
                    kind="extend_deadline",
                    description=f"Move {pid}'s target date to {new_date.isoformat()}",
                    project_id=pid,
                    target_date=new_date,
                )

    # Structural: max block length, first rung of the ladder that recovers time.
    for pid in short:
        d = base.demands[pid]
        p = d.project
        longest = min(DAY, max((clk.down(e) - clk.up(s) for s, e in d.base), default=0))
        ladder = sorted(
            {
                min(longest, _ceil_div(p.max_block_minutes * 3, 2 * GRID) * GRID),
                min(longest, p.max_block_minutes * 2),
                longest,
            }
        )
        for proposed in (x for x in ladder if x > p.max_block_minutes):
            if offer(
                counterfactual(projects=with_project(pid, max_block_minutes=proposed)),
                kind="raise_max_block_minutes",
                description=f"Allow {pid} sittings up to {proposed} min",
                project_id=pid,
                max_block_minutes=proposed,
            ):
                break

    # Removals: at most MAX_PIN_COUNTERFACTUALS candidates per kind, ranked by how much of
    # the short projects' allowed time (before exceptions and forbids) they occupy.
    short_projects = [base.demands[pid].project for pid in short]
    open_time = {p.id: _base_allowed(p, inp, clk, removable=False) for p in short_projects}

    def reach(span: Span, projects: Iterable[ProjectInput]) -> int:
        return sum(_overlap(span, open_time[q.id]) for q in projects)

    def top(scored: Iterable[tuple[int, str]]) -> list[str]:
        ranked = sorted((-n, key) for n, key in scored if n > 0)
        return [key for _, key in ranked[:MAX_PIN_COUNTERFACTUALS]]

    exc_by_id = {e.id: e for e in inp.exceptions}
    for eid in top(
        (reach(clk.span(e.span), [q for q in short_projects if _blocks_project(e, q)]), e.id)
        for e in exc_by_id.values()
    ):
        offer(
            counterfactual(exceptions=tuple(x for x in inp.exceptions if x.id != eid)),
            kind="remove_exception",
            description=f"Drop availability exception {eid}",
            exception_id=eid,
        )

    placed_pin_ids = {i for b in base.blocks for i in b.pin_ids}
    pin_reach: dict[str, int] = {}
    for pin in inp.pins:
        if pin.kind == "forbid":
            hit = [q for q in short_projects if _forbids_project(pin, q.id)]
        elif pin.id in placed_pin_ids and pin.project_id not in short:
            hit = short_projects
        else:
            continue
        pin_reach[pin.id] = pin_reach.get(pin.id, 0) + reach(clk.span(pin.span), hit)
    kinds = {pin.id: pin.kind for pin in inp.pins}
    for pin_id in top((n, i) for i, n in pin_reach.items()):
        offer(
            counterfactual(pins=tuple(x for x in inp.pins if x.id != pin_id)),
            kind="remove_pin",
            description=f"Remove {kinds[pin_id]} {pin_id}",
            pin_id=pin_id,
        )

    pause_reach: dict[str, int] = {}
    for blk in base.blocks:
        if blk.project_id not in short:
            n = reach(blk.span, short_projects) or 1  # any scheduled time may free capacity
            pause_reach[blk.project_id] = pause_reach.get(blk.project_id, 0) + n
    for qid in top((n, q) for q, n in pause_reach.items()):
        offer(
            counterfactual(
                projects=tuple(q for q in inp.projects if q.id != qid),
                pins=tuple(x for x in inp.pins if x.project_id != qid),
            ),
            exclude=qid,
            kind="pause_project",
            description=f"Pause {qid} this week",
            project_id=qid,
        )

    out.sort(
        key=lambda r: (
            -r.recovers_minutes,
            r.kind,
            r.project_id or "",
            r.pin_id or "",
            r.exception_id or "",
        )
    )
    # One relaxation per kind per short project: a removal survives only as the best of its
    # kind for some short project it helps (demand kinds and raise_max are one per project).
    kept: list[Relaxation] = []
    claimed: set[tuple[str, str]] = set()
    for r in out:
        owners = {r.project_id or ""} if r.project_id in short else set(r.affected_ids)
        fresh = {(r.kind, o) for o in owners} - claimed
        if fresh:
            claimed |= fresh
            kept.append(r)
    return kept


# ------------------------------------------------------------------------- entry


def pack(input: SolverInput, previous: Sequence[PlannedBlock] | None = None) -> SolverOutput:
    """Pack the remainder of the horizon; always returns the best plan, never raises on
    infeasibility. `previous` is the plan being replaced, used only for lineage reuse and
    stability."""
    inp = _canonical(input)
    prev = sorted(previous or (), key=lambda b: (b.span.start, b.span.end, b.lineage_id))
    result = _solve(inp, prev)
    clk = _Clock(inp)

    blocks = tuple(
        PlannedBlock(
            lineage_id=b.lineage_id or "",
            project_id=b.project_id,
            span=clk.interval(b.span),
            pinned=b.pinned,
        )
        for b in result.blocks
    )

    violations: list[InfeasibilityViolation] = list(result.invalidated)
    for pid in sorted(result.demands):
        s = result.shortfall(pid)
        if s > 0:
            d = result.demands[pid]
            violations.append(
                DemandShortfall(
                    project_id=pid,
                    required_minutes=d.required,
                    scheduled_minutes=d.required - s,
                    shortfall_minutes=s,
                )
            )
    raw = {
        (v.kind, v.block_lineage_ids, v.related_ids, v.message): v
        for v in [*result.trial_violations, *verify(blocks, inp)]
    }
    verify_violations = tuple(raw[k] for k in sorted(raw))

    infeasibility = None
    if violations or verify_violations:
        relax = (
            _relaxations(inp, prev, result)
            if any(v.kind == "demand_shortfall" for v in violations)
            else []
        )
        infeasibility = Infeasibility(
            violations=tuple(violations),
            relaxations=tuple(relax),
            verify_violations=verify_violations,
        )
    return SolverOutput(
        input_schema_version=inp.input_schema_version,
        solver_version=SOLVER_VERSION,
        blocks=blocks,
        infeasibility=infeasibility,
    )

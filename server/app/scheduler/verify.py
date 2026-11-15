"""Plan verifier (ARCHITECTURE.md §4): the oracle every produced plan must pass.

Pure and deterministic: the plan is canonically sorted before checking, so the
result does not depend on the order of the input list. Spans are half-open
[start, end); touching intervals do not overlap.

Violation kinds, in output sort order: before_now, block_length, buffer, busy,
duplicate_lineage, exception, forbid, horizon, overlap, pin_missing,
pin_unmatched, preferred_window, unknown_project, window. Output is sorted by
kind, then block lineage ids.

Pins (contracts D7/O6) are matched by exact endpoints, never containment: a
planned block realizes a `pin`-kind Pin iff it has the pin's project_id and
exactly the pin's span. A realized pin skips `block_length`; every other check
still applies, and a violation on a realizing block carries the pin id in
`related_ids` (derived from the matched input pin, not from `pinned`) so the
caller can report it as `pin_invalidated`. `PlannedBlock.pinned` must agree
with the inputs, otherwise `pin_unmatched`.

Frozen blocks are the represented past: they are exempt from `before_now` and
from the per-block checks, but count for overlap, buffer, pin realization, and
lineage uniqueness. Strict preferred windows are hard (`preferred_window`);
non-strict windows produce nothing here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from .models import (
    FrozenBlock,
    Interval,
    Pin,
    PlannedBlock,
    ProjectInput,
    SolverInput,
    Violation,
    ViolationKind,
)


def _block_key(b: PlannedBlock) -> tuple[datetime, datetime, str, str, bool]:
    return (b.span.start, b.span.end, b.lineage_id, b.project_id, b.pinned)


def _merge(intervals: Iterable[Interval]) -> list[tuple[datetime, datetime]]:
    """Union of intervals as sorted (start, end) pairs; touching intervals merge."""
    merged: list[tuple[datetime, datetime]] = []
    for iv in sorted(intervals, key=lambda i: (i.start, i.end)):
        if merged and iv.start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], iv.end))
        else:
            merged.append((iv.start, iv.end))
    return merged


def _fmt(iv: Interval) -> str:
    return f"[{iv.start.isoformat()}, {iv.end.isoformat()})"


def _realizes(item: PlannedBlock | FrozenBlock, pin: Pin) -> bool:
    """Exact-endpoint match: same project and identical span."""
    return pin.kind == "pin" and item.project_id == pin.project_id and item.span == pin.span


def _realized_pin_ids(block: PlannedBlock, pins: Sequence[Pin]) -> list[str]:
    """Ids of the input `pin`-kind pins this block exactly realizes."""
    return [p.id for p in pins if _realizes(block, p)]


def _inside(span: Interval, merged: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(s <= span.start and span.end <= e for s, e in merged)


def verify(plan: Sequence[PlannedBlock], input: SolverInput) -> list[Violation]:
    """Return every constraint violation in `plan`; [] means the plan is valid."""
    blocks = sorted(plan, key=_block_key)
    projects: dict[str, ProjectInput] = {p.id: p for p in input.projects}
    pins = sorted(input.pins, key=lambda p: (p.id, p.kind, p.span.start, p.span.end))
    exceptions = sorted(input.exceptions, key=lambda e: (e.id, e.span.start, e.span.end))
    frozen = sorted(input.frozen, key=lambda f: (f.span.start, f.span.end, f.lineage_id))
    schedulable = _merge(input.schedulable)

    out: list[Violation] = []

    def add(
        kind: ViolationKind,
        message: str,
        lineage_ids: Iterable[str] = (),
        related: Iterable[str] = (),
        pinned_blocks: Iterable[PlannedBlock] = (),
    ) -> None:
        rel = list(related)
        for b in pinned_blocks:
            rel.extend(_realized_pin_ids(b, pins))
        out.append(
            Violation(
                kind=kind,
                message=message,
                block_lineage_ids=tuple(lineage_ids),
                related_ids=tuple(rel),
            )
        )

    preferred = {p.id: _merge(p.preferred_windows) for p in input.projects}

    # Per-block checks.
    for b in blocks:
        project = projects.get(b.project_id)
        realized = _realized_pin_ids(b, pins)

        if b.span.start < input.now:
            add(
                "before_now",
                f"block {b.lineage_id} {_fmt(b.span)} starts before now "
                f"{input.now.isoformat()}; the past is immutable",
                [b.lineage_id],
                pinned_blocks=[b],
            )

        if b.pinned and not realized:
            add(
                "pin_unmatched",
                f"block {b.lineage_id} {_fmt(b.span)} is marked pinned but no input pin "
                f"for project {b.project_id} has exactly this span",
                [b.lineage_id],
                [b.project_id],
            )
        elif realized and not b.pinned:
            add(
                "pin_unmatched",
                f"block {b.lineage_id} exactly realizes pin(s) {', '.join(realized)} "
                "but is not marked pinned",
                [b.lineage_id],
                realized,
            )

        if (
            project is not None
            and project.preferred_windows_strict
            and not _inside(b.span, preferred[project.id])
        ):
            add(
                "preferred_window",
                f"block {b.lineage_id} {_fmt(b.span)} is outside the strict preferred "
                f"windows of project {project.id}",
                [b.lineage_id],
                [project.id],
                [b],
            )

        if project is None:
            add(
                "unknown_project",
                f"block {b.lineage_id} references unknown project {b.project_id}",
                [b.lineage_id],
                [b.project_id],
                [b],
            )

        if not input.horizon.contains(b.span):
            add(
                "horizon",
                f"block {b.lineage_id} {_fmt(b.span)} lies outside horizon {_fmt(input.horizon)}",
                [b.lineage_id],
                pinned_blocks=[b],
            )

        if not _inside(b.span, schedulable):
            add(
                "window",
                f"block {b.lineage_id} {_fmt(b.span)} is not inside a schedulable window",
                [b.lineage_id],
                pinned_blocks=[b],
            )

        busy_hits = sorted(
            (iv for iv in input.busy if iv.overlaps(b.span)), key=lambda i: (i.start, i.end)
        )
        if busy_hits:
            add(
                "busy",
                f"block {b.lineage_id} {_fmt(b.span)} intersects busy time "
                + ", ".join(_fmt(iv) for iv in busy_hits),
                [b.lineage_id],
                pinned_blocks=[b],
            )

        req = set(project.requirement_tags) if project is not None else set()
        exc_hits = [
            e.id
            for e in exceptions
            if e.span.overlaps(b.span) and (e.blocks_all or req.intersection(e.tags))
        ]
        if exc_hits:
            add(
                "exception",
                f"block {b.lineage_id} intersects availability exception(s) {', '.join(exc_hits)}",
                [b.lineage_id],
                exc_hits,
                [b],
            )

        forbid_hits = [
            p.id
            for p in pins
            if p.kind == "forbid"
            and p.span.overlaps(b.span)
            and (p.project_id is None or p.project_id == b.project_id)
        ]
        if forbid_hits:
            add(
                "forbid",
                f"block {b.lineage_id} intersects forbid pin(s) {', '.join(forbid_hits)}",
                [b.lineage_id],
                forbid_hits,
                [b],
            )

        if project is not None and not realized:
            length = b.span.duration_minutes
            if length > project.max_block_minutes:
                add(
                    "block_length",
                    f"block {b.lineage_id} lasts {length} > max {project.max_block_minutes} min",
                    [b.lineage_id],
                    [project.id],
                )
            elif length < project.min_block_minutes:
                add(
                    "block_length",
                    f"block {b.lineage_id} lasts {length} < min {project.min_block_minutes} min",
                    [b.lineage_id],
                    [project.id],
                )

    # Lineage identity: unique among planned blocks, and never reusing a frozen lineage.
    planned_counts = Counter(b.lineage_id for b in blocks)
    frozen_ids = {f.lineage_id for f in frozen}
    for lid in sorted(planned_counts):
        n = planned_counts[lid]
        if n > 1 or lid in frozen_ids:
            where = f"{n} planned blocks" if n > 1 else "a planned block"
            also = " and a frozen block" if lid in frozen_ids else ""
            add(
                "duplicate_lineage",
                f"lineage {lid} is used by {where}{also}",
                [lid],
            )

    # Pairwise checks: planned vs planned, and planned vs frozen.

    def pair(
        a_id: str, a_span: Interval, b_id: str, b_span: Interval, pinned: list[PlannedBlock]
    ) -> None:
        ids = sorted([a_id, b_id])
        if a_span.overlaps(b_span):
            add(
                "overlap",
                f"blocks {ids[0]} and {ids[1]} overlap",
                ids,
                pinned_blocks=pinned,
            )
            return
        first, second = (a_span, b_span) if a_span.start <= b_span.start else (b_span, a_span)
        gap = (second.start - first.end) // timedelta(minutes=1)
        if gap < input.buffer_minutes:
            add(
                "buffer",
                f"blocks {ids[0]} and {ids[1]} are {gap} min apart (< {input.buffer_minutes} min)",
                ids,
                pinned_blocks=pinned,
            )

    for i, a in enumerate(blocks):
        for b in blocks[i + 1 :]:
            pair(a.lineage_id, a.span, b.lineage_id, b.span, [a, b])
        for f in frozen:
            pair(a.lineage_id, a.span, f.lineage_id, f.span, [a])

    # Pins must be realized: a planned or frozen block with exactly the pin's project and span.
    for p in pins:
        if p.kind != "pin" or p.project_id is None:
            continue
        if any(_realizes(b, p) for b in blocks) or any(_realizes(f, p) for f in frozen):
            continue
        add(
            "pin_missing",
            f"pin {p.id} for project {p.project_id} {_fmt(p.span)} has no block with exactly "
            "this span",
            related=[p.id, p.project_id],
        )

    unique = {(v.kind, v.block_lineage_ids, v.related_ids, v.message): v for v in out}
    return [unique[k] for k in sorted(unique)]

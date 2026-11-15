"""Pure DB-row → response-body mappers (contracts/openapi.yaml; NOTES "Responses vs DDL").

No I/O: every function takes ORM instances / plain values and returns JSON-ready dicts, so
they are unit-testable without a database. Conventions: tstzrange → `{start, end}`, UTC `Z`
timestamps, numeric(·,1) → float, internal columns (user_id, solver_input, gcal_etag) omitted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import Range

from app.db.models import AvailabilityException, Block, BlockPin, Changeset, Project, WeekPlan
from app.db.models import UserPreferences as PreferencesRow

# ---------------------------------------------------------------- primitives


def ts(value: datetime) -> str:
    """RFC 3339 in UTC with a `Z` suffix (fractional seconds kept only when present)."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def opt_ts(value: datetime | None) -> str | None:
    return None if value is None else ts(value)


def span(r: Range[datetime]) -> dict[str, str]:
    assert r.lower is not None and r.upper is not None, "unbounded tstzrange"
    return {"start": ts(r.lower), "end": ts(r.upper)}


def _num(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def _iso(v: date | None) -> str | None:
    return None if v is None else v.isoformat()


# ---------------------------------------------------------------- rows


def project_out(p: Project) -> dict[str, Any]:
    """D9: both kinds' fields present; the other kind's are null."""
    return {
        "id": str(p.id),
        "name": p.name,
        "kind": p.kind,
        "target_date": _iso(p.target_date),
        "estimated_total_hours": _num(p.estimated_total_hours),
        "weekly_target_hours": _num(p.weekly_target_hours),
        "max_block_minutes": p.max_block_minutes,
        "min_block_minutes": p.min_block_minutes,
        "preferred_windows": p.preferred_windows,
        "preferred_strict": p.preferred_strict,
        "requirements": list(p.requirements),
        "status": p.status,
        "created_at": ts(p.created_at),
    }


def preferences_out(p: PreferencesRow) -> dict[str, Any]:
    return {
        "timezone": p.timezone,
        "schedulable_windows": p.schedulable_windows,
        "day_diversity": p.day_diversity,
        "buffer_minutes": p.buffer_minutes,
        "week_start": p.week_start,
    }


def exception_out(e: AvailabilityException) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "span": span(e.span),
        "blocks_all": e.blocks_all,
        "blocks_tags": list(e.blocks_tags),
        "reason": e.reason,
        "created_at": ts(e.created_at),
    }


def pin_out(p: BlockPin) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "project_id": None if p.project_id is None else str(p.project_id),
        "span": span(p.span),
        "kind": p.kind,
        "created_from_block_id": (
            None if p.created_from_block_id is None else str(p.created_from_block_id)
        ),
        "created_at": ts(p.created_at),
    }


def block_out(b: Block) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "plan_id": str(b.plan_id),
        "project_id": str(b.project_id),
        "lineage_id": str(b.lineage_id),
        "span": span(b.span),
        "state": b.state,
        "gcal_status": b.gcal_status,
        "gcal_event_id": b.gcal_event_id,
    }


def _block_key(b: Block) -> tuple[datetime, str]:
    assert b.span.lower is not None
    return (b.span.lower, str(b.lineage_id))


def plan_out(
    plan: WeekPlan, blocks: Iterable[Block], completed_minutes: Mapping[str, int]
) -> dict[str, Any]:
    """WeekPlan + its blocks ordered by span.start (lineage id breaks ties)."""
    return {
        "id": str(plan.id),
        "horizon": span(plan.horizon),
        "week_start": plan.week_start.isoformat(),
        "timezone": plan.timezone,
        "version": plan.version,
        "status": plan.status,
        "infeasibility": infeasibility_out(plan.infeasibility, completed_minutes),
        "created_at": ts(plan.created_at),
        "superseded_at": opt_ts(plan.superseded_at),
        "blocks": [block_out(b) for b in sorted(blocks, key=_block_key)],
    }


# ---------------------------------------------------------------- infeasibility (D17)

_HOURS_STEP = Decimal("0.1")
_RELAX_FIELDS: dict[str, tuple[str, ...]] = {
    "extend_deadline": ("project_id", "target_date"),
    "reduce_estimate": ("project_id",),
    "reduce_weekly_target": ("project_id",),
    "raise_max_block_minutes": ("project_id", "max_block_minutes"),
    "pause_project": ("project_id",),
    "remove_pin": ("pin_id",),
    "remove_exception": ("exception_id",),
}


def minutes_to_hours(minutes: int, upper: Decimal) -> float:
    """Integer minutes → hours at one decimal, rounded DOWN (a relaxation must not ask for
    more than the solver found room for), clamped to (0, upper]."""
    hours = (Decimal(minutes) / 60).quantize(_HOURS_STEP, rounding=ROUND_FLOOR)
    return float(min(max(hours, _HOURS_STEP), upper))


def _span_json(raw: Mapping[str, str]) -> dict[str, str]:
    return {
        "start": ts(datetime.fromisoformat(raw["start"])),
        "end": ts(datetime.fromisoformat(raw["end"])),
    }


def _violation_out(v: Mapping[str, Any]) -> dict[str, Any]:
    if v["kind"] == "demand_shortfall":
        keys = ("kind", "project_id", "required_minutes", "scheduled_minutes", "shortfall_minutes")
        return {k: v[k] for k in keys}
    return {
        "kind": "pin_invalidated",
        "pin_id": v["pin_id"],
        "pin_kind": v["pin_kind"],
        "project_id": v.get("project_id"),
        "span": _span_json(v["span"]),
        "conflicts_with": v["conflicts_with"],
        "exception_id": v.get("exception_id"),
        "related_lineage_ids": list(v.get("related_lineage_ids", [])),
    }


def relaxation_out(r: Mapping[str, Any], completed_minutes: Mapping[str, int]) -> dict[str, Any]:
    """Solver units → the D17 PATCH-body units: remaining_minutes + completed minutes →
    estimated_total_hours; weekly_target_minutes → weekly_target_hours."""
    kind = r["kind"]
    out: dict[str, Any] = {"kind": kind, "recovers_minutes": r["recovers_minutes"]}
    for name in _RELAX_FIELDS[kind]:
        out[name] = r[name]
    if kind == "reduce_estimate":
        # TODO(contract): completed minutes are read at response time, not at solve time; a
        # complete/skip between the two shifts the proposed estimate by that block.
        total = r["remaining_minutes"] + completed_minutes.get(r["project_id"], 0)
        out["estimated_total_hours"] = minutes_to_hours(total, Decimal("99999.9"))
    elif kind == "reduce_weekly_target":
        out["weekly_target_hours"] = minutes_to_hours(r["weekly_target_minutes"], Decimal("168"))
    return out


def infeasibility_out(
    raw: Mapping[str, Any] | None, completed_minutes: Mapping[str, int]
) -> dict[str, Any] | None:
    """Stored scheduler `Infeasibility` → contract shape (`verify_violations` dropped). An
    infeasibility with no violations maps to null (the contract requires minItems 1)."""
    if not raw or not raw.get("violations"):
        return None
    return {
        "violations": [_violation_out(v) for v in raw["violations"]],
        "relaxations": [relaxation_out(r, completed_minutes) for r in raw.get("relaxations", [])],
    }


# ---------------------------------------------------------------- diff (D18)


@dataclass(frozen=True)
class DiffBlock:
    lineage_id: str
    project_id: str
    start: datetime
    end: datetime

    @classmethod
    def of(cls, b: Block) -> DiffBlock:
        assert b.span.lower is not None and b.span.upper is not None
        return cls(str(b.lineage_id), str(b.project_id), b.span.lower, b.span.upper)

    def span(self) -> dict[str, str]:
        return {"start": ts(self.start), "end": ts(self.end)}


def diff_out(
    plan_id: str,
    against_plan_id: str | None,
    after: Sequence[DiffBlock],
    before: Sequence[DiffBlock],
) -> dict[str, Any]:
    """One entry per lineage in either plan, ordered by (coalesce(after.start, before.start),
    lineage_id). `moved` = span changed (includes resize)."""
    a = {b.lineage_id: b for b in after}
    b4 = {b.lineage_id: b for b in before}
    entries: list[tuple[datetime, str, dict[str, Any]]] = []
    for lid in a.keys() | b4.keys():
        new, old = a.get(lid), b4.get(lid)
        if new is not None and old is not None:
            same = (new.start, new.end) == (old.start, old.end)
            change = "unchanged" if same else "moved"
        else:
            change = "added" if new is not None else "removed"
        ref = new if new is not None else old
        assert ref is not None
        entry = {
            "lineage_id": lid,
            "project_id": ref.project_id,
            "before": None if old is None else old.span(),
            "after": None if new is None else new.span(),
            "change": change,
        }
        entries.append((ref.start, lid, entry))
    entries.sort(key=lambda e: (e[0], e[1]))
    return {
        "plan_id": plan_id,
        "against_plan_id": against_plan_id,
        "entries": [e for _, _, e in entries],
    }


# ---------------------------------------------------------------- changesets


def staged_out(staged: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """`changesets.staged` jsonb → StagedMutation list. Draft fields the contract types as
    non-nullable are dropped when unset (`min_block_minutes: null` → absent; the effective
    value is LEAST(25, max_block_minutes), applied on confirm)."""
    out: list[dict[str, Any]] = []
    for m in staged:
        item = dict(m)
        if item.get("kind") == "project_created":
            payload = dict(item["payload"])
            payload["project"] = {
                k: v
                for k, v in payload["project"].items()
                if not (v is None and k == "min_block_minutes")
            }
            item["payload"] = payload
        out.append(item)
    return out


def changeset_out(
    cs: Changeset,
    plan: dict[str, Any] | None,
    diff: dict[str, Any] | None,
    narration: str | None = None,
) -> dict[str, Any]:
    """`plan_id`, `plan` and `diff` are all set or all null."""
    return {
        "id": str(cs.id),
        "status": cs.status,
        "plan_id": None if cs.plan_id is None else str(cs.plan_id),
        "staged": staged_out(cs.staged),
        "created_at": ts(cs.created_at),
        "resolved_at": opt_ts(cs.resolved_at),
        "plan": plan,
        "diff": diff,
        "narration": narration,
    }

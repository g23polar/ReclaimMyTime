"""Postgres enum types from ARCHITECTURE.md §3 DDL (value order matches the DDL)."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import ENUM

CONNECTION_STATUS = ("active", "revoked")
PROJECT_KIND = ("deadline", "perennial")
PROJECT_STATUS = ("staged", "active", "paused", "done", "archived")
DAY_DIVERSITY = ("focused", "mixed")
PIN_KIND = ("pin", "forbid")
PLAN_STATUS = ("proposed", "live", "superseded", "rejected")
CHANGESET_STATUS = ("pending", "confirmed", "rejected", "expired")
# Why a changeset was proposed (migration 0002, T4.3 contract 2026-11-16).
CHANGESET_REASON = ("user_edit", "agent", "drift", "google_edit")
BLOCK_STATE = ("planned", "completed", "skipped")
GCAL_SYNC = ("pending", "synced", "failed")
MUTATION_SOURCE = ("agent", "manual", "system")
# Todos v1 (migration 0005). Direct-write table, so no `mutations` kind exists for it.
TODO_KIND = ("daily", "weekly", "one_off")
MUTATION_KIND = (
    "project_created",
    "project_updated",
    "project_archived",
    "preferences_updated",
    "exception_added",
    "exception_removed",
    "pin_added",
    "pin_removed",
    "plan_proposed",
    "plan_confirmed",
    "plan_rejected",
)

# Every enum, in DDL creation order (the migration creates/drops the same names).
ENUMS: dict[str, tuple[str, ...]] = {
    "connection_status": CONNECTION_STATUS,
    "project_kind": PROJECT_KIND,
    "project_status": PROJECT_STATUS,
    "day_diversity": DAY_DIVERSITY,
    "pin_kind": PIN_KIND,
    "plan_status": PLAN_STATUS,
    "changeset_status": CHANGESET_STATUS,
    "changeset_reason": CHANGESET_REASON,
    "block_state": BLOCK_STATE,
    "gcal_sync": GCAL_SYNC,
    "mutation_source": MUTATION_SOURCE,
    "mutation_kind": MUTATION_KIND,
    "todo_kind": TODO_KIND,
}


_TYPES: dict[str, ENUM] = {name: ENUM(*values, name=name) for name, values in ENUMS.items()}


def pg_enum(name: str) -> ENUM:
    """The shared ENUM type object for `name` (one instance per type, reused by every column)."""
    return _TYPES[name]

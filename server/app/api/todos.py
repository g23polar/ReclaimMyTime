"""Todos (`todos` tag, Todos v1): direct CRUD, no changeset, no solve, no `mutations` row.

Rules live here; rows are read and written by app.db.todos. Periods are computed from
X-Timezone (the local date) and preferences.week_start (default 1 when no preferences row exists).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    DeviceTz,
    Now,
    RawBody,
    Tx,
    UserId,
    not_found,
    parse_body,
    validation_failed,
)
from app.api.mappers import todo_out
from app.api.rate_limit import LimitWrites
from app.auth.dependencies import require_timezone
from app.db import todos as repo
from app.db.models import UserPreferences
from app.db.todos import TodoState
from app.errors import AppError
from app.todos.periods import RECURRING_KINDS, allowed_periods

router = APIRouter(tags=["todos"], dependencies=[Depends(require_timezone)])

TodoKind = Literal["daily", "weekly", "one_off"]
_DEFAULT_WEEK_START = 1  # the user_preferences column default; used when no row exists yet


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TodoDraft(_Body):
    kind: TodoKind = "one_off"
    title: Annotated[str, Field(min_length=1, max_length=200)]
    notes: Annotated[str, Field(max_length=2000)] | None = None
    due_date: date | None = None


class TodoPatch(_Body):
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None
    due_date: date | None = None
    done: bool | None = None


class TodoOrder(_Body):
    kind: TodoKind
    ids: list[uuid.UUID]

    @field_validator("ids")
    @classmethod
    def _unique(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(v)) != len(v):
            raise ValueError("ids must be unique")
        return v


_DRAFT: TypeAdapter[TodoDraft] = TypeAdapter(TodoDraft)
_PATCH: TypeAdapter[TodoPatch] = TypeAdapter(TodoPatch)
_ORDER: TypeAdapter[TodoOrder] = TypeAdapter(TodoOrder)


@dataclass(frozen=True)
class _Calendar:
    """The device's local today and the user's week start: everything period-related."""

    today: date
    week_start: int

    @property
    def allowed(self) -> dict[str, tuple[date, date]]:
        return {k: allowed_periods(k, self.today, self.week_start) for k in RECURRING_KINDS}

    def periods(self) -> dict[str, str]:
        daily, weekly = self.allowed["daily"][0], self.allowed["weekly"][0]
        return {"daily": daily.isoformat(), "weekly": weekly.isoformat()}


async def _calendar(tx: AsyncSession, user_id: uuid.UUID, now: datetime, tz: ZoneInfo) -> _Calendar:
    week_start = (
        await tx.execute(
            select(UserPreferences.week_start).where(UserPreferences.user_id == user_id)
        )
    ).scalar_one_or_none()
    return _Calendar(
        today=now.astimezone(tz).date(),
        week_start=_DEFAULT_WEEK_START if week_start is None else int(week_start),
    )


def _list_payload(states: list[TodoState], cal: _Calendar) -> dict[str, Any]:
    return {"todos": [todo_out(s) for s in states], "periods": cal.periods()}


async def _state_or_404(
    tx: AsyncSession, user_id: uuid.UUID, todo_id: uuid.UUID, cal: _Calendar
) -> TodoState:
    state = await repo.get_state(tx, user_id, todo_id, allowed=cal.allowed)
    if state is None:
        raise not_found("todo", todo_id)
    return state


def _recurring_conflict(state: TodoState) -> AppError:
    return AppError(
        409,
        "todo_recurring",
        "Recurring todos are ticked per period, not marked done.",
        {"resource": "todo", "id": str(state.row.id), "kind": state.row.kind},
    )


@router.get("/todos", operation_id="listTodos")
async def list_todos(
    user_id: UserId,
    tx: Tx,
    now: Now,
    tz: DeviceTz,
    include_done: bool = Query(False),
) -> dict[str, Any]:
    cal = await _calendar(tx, user_id, now, tz)
    states = await repo.list_states(
        tx, user_id, now=now, include_done=include_done, allowed=cal.allowed
    )
    return _list_payload(states, cal)


@router.post("/todos", operation_id="createTodo", status_code=201, dependencies=[LimitWrites])
async def create_todo(
    user_id: UserId, tx: Tx, now: Now, tz: DeviceTz, body: RawBody
) -> dict[str, Any]:
    draft = parse_body(_DRAFT, body)
    cal = await _calendar(tx, user_id, now, tz)

    due = draft.due_date
    if draft.kind == "one_off":
        if "due_date" not in draft.model_fields_set:
            due = cal.today  # server default: today's local date
    elif draft.due_date is not None:
        raise validation_failed([{"path": "due_date", "message": "only valid for a one_off todo"}])

    todo_id = await repo.create(
        tx,
        user_id,
        kind=draft.kind,
        title=draft.title,
        notes=draft.notes,
        due_date=due,
        now=now,
    )
    state = await _state_or_404(tx, user_id, todo_id, cal)
    return todo_out(state)


@router.put("/todos/order", operation_id="reorderTodos", dependencies=[LimitWrites])
async def reorder_todos(
    user_id: UserId, tx: Tx, now: Now, tz: DeviceTz, body: RawBody
) -> dict[str, Any]:
    order = parse_body(_ORDER, body)
    cal = await _calendar(tx, user_id, now, tz)
    current = await repo.open_ids_of_kind(tx, user_id, order.kind)
    missing = [str(i) for i in current if i not in set(order.ids)]
    extra = [str(i) for i in order.ids if i not in set(current)]
    if missing or extra:
        raise AppError(
            422,
            "invalid_order",
            "ids must be exactly the open todos of this kind.",
            {"kind": order.kind, "missing": missing, "extra": extra},
        )
    await repo.rewrite_positions(tx, user_id, order.ids, now=now)
    states = await repo.list_states(tx, user_id, now=now, include_done=False, allowed=cal.allowed)
    return _list_payload(states, cal)


@router.patch("/todos/{todo_id}", operation_id="updateTodo", dependencies=[LimitWrites])
async def update_todo(
    user_id: UserId, tx: Tx, now: Now, tz: DeviceTz, todo_id: uuid.UUID, body: RawBody
) -> dict[str, Any]:
    patch = parse_body(_PATCH, body)
    if not patch.model_fields_set:
        raise validation_failed([{"path": "", "message": "at least one field is required"}])
    cal = await _calendar(tx, user_id, now, tz)
    state = await _state_or_404(tx, user_id, todo_id, cal)
    row = state.row
    recurring = row.kind != "one_off"
    fields = patch.model_fields_set

    for name in ("title", "done"):
        if name in fields and getattr(patch, name) is None:
            raise validation_failed([{"path": name, "message": "must not be null"}])
    if "done" in fields and recurring:
        raise _recurring_conflict(state)
    if recurring and patch.due_date is not None:
        raise _recurring_conflict(state)

    values: dict[str, Any] = {}
    if "title" in fields:
        values["title"] = patch.title
    if "notes" in fields:
        values["notes"] = patch.notes
    if "due_date" in fields and not recurring:
        values["due_date"] = patch.due_date
    if "done" in fields and not recurring:
        if patch.done and row.completed_at is None:
            values["completed_at"] = now
        elif not patch.done and row.completed_at is not None:
            # Undoing a one-off moves it to the end of the open order (the done one-off kept its
            # position while done).
            values["completed_at"] = None
            values["position"] = await repo.next_open_one_off_position(tx, user_id)

    if values:
        await repo.update_todo(tx, user_id, todo_id, values, now=now)
    updated = await _state_or_404(tx, user_id, todo_id, cal)
    return todo_out(updated)


@router.delete(
    "/todos/{todo_id}", operation_id="deleteTodo", status_code=204, dependencies=[LimitWrites]
)
async def delete_todo(user_id: UserId, tx: Tx, todo_id: uuid.UUID) -> Response:
    if not await repo.delete_todo(tx, user_id, todo_id):
        raise not_found("todo", todo_id)
    return Response(status_code=204)


async def _period_target(
    tx: AsyncSession,
    user_id: uuid.UUID,
    now: datetime,
    tz: ZoneInfo,
    todo_id: uuid.UUID,
    period_start: date,
) -> tuple[TodoState, _Calendar]:
    """Shared checks for both completion routes: the todo exists, is recurring, and the period
    is the current or previous start for its kind."""
    cal = await _calendar(tx, user_id, now, tz)
    state = await _state_or_404(tx, user_id, todo_id, cal)
    kind = state.row.kind
    if kind == "one_off":
        raise AppError(
            409,
            "todo_not_recurring",
            "Only recurring todos have period completions.",
            {"resource": "todo", "id": str(todo_id), "kind": kind},
        )
    allowed = cal.allowed[kind]
    if period_start not in allowed:
        raise AppError(
            422,
            "invalid_period",
            "period_start must be the current or previous period start for this kind.",
            {
                "kind": kind,
                "period_start": period_start.isoformat(),
                "allowed": [p.isoformat() for p in allowed],
            },
        )
    return state, cal


@router.put(
    "/todos/{todo_id}/completions/{period_start}",
    operation_id="completeTodoPeriod",
    dependencies=[LimitWrites],
)
async def complete_todo_period(
    user_id: UserId,
    tx: Tx,
    now: Now,
    tz: DeviceTz,
    todo_id: uuid.UUID,
    period_start: date,
) -> dict[str, Any]:
    _, cal = await _period_target(tx, user_id, now, tz, todo_id, period_start)
    await repo.tick(tx, todo_id, period_start, now)
    return todo_out(await _state_or_404(tx, user_id, todo_id, cal))


@router.delete(
    "/todos/{todo_id}/completions/{period_start}",
    operation_id="uncompleteTodoPeriod",
    dependencies=[LimitWrites],
)
async def uncomplete_todo_period(
    user_id: UserId,
    tx: Tx,
    now: Now,
    tz: DeviceTz,
    todo_id: uuid.UUID,
    period_start: date,
) -> dict[str, Any]:
    _, cal = await _period_target(tx, user_id, now, tz, todo_id, period_start)
    await repo.untick(tx, todo_id, period_start)
    return todo_out(await _state_or_404(tx, user_id, todo_id, cal))

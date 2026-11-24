"""Todos repository (Todos v1): direct CRUD, no changeset, no solve, no `mutations` row.

Deliberate exception to "writes go through app.db.write_paths" (see app.db.models.intent). The
API layer (app.api.todos) owns the rules (kind immutability, project link checks, which periods
are allowed); this module only reads and writes rows, scoped to `user_id` on every statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, Todo, TodoCompletion

# A one-off done more than this long ago drops out of listTodos unless include_done is set.
DONE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class TodoState:
    """A todo row plus what the API reports about it."""

    row: Todo
    project_archived: bool
    completed_periods: tuple[date, ...]  # recurring only; sorted; ⊆ the allowed periods


async def list_states(
    tx: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime,
    include_done: bool,
    allowed: Mapping[str, Sequence[date]],
) -> list[TodoState]:
    """Todos for the user, in (kind, position, created_at, id) order. `allowed` maps each
    recurring kind to its current and previous period starts."""
    query = (
        select(Todo, Project.status)
        .outerjoin(Project, Project.id == Todo.project_id)
        .where(Todo.user_id == user_id)
    )
    if not include_done:
        query = query.where(
            (Todo.kind != "one_off")
            | Todo.completed_at.is_(None)
            | (Todo.completed_at >= now - DONE_WINDOW)
        )
    query = query.order_by(Todo.kind, Todo.position, Todo.created_at, Todo.id)
    pairs = [(row, status) for row, status in (await tx.execute(query)).all()]
    return await _with_completions(tx, pairs, allowed)


async def get_state(
    tx: AsyncSession,
    user_id: uuid.UUID,
    todo_id: uuid.UUID,
    *,
    allowed: Mapping[str, Sequence[date]],
) -> TodoState | None:
    query = (
        select(Todo, Project.status)
        .outerjoin(Project, Project.id == Todo.project_id)
        .where(Todo.id == todo_id, Todo.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    found = (await tx.execute(query)).first()
    if found is None:
        return None
    states = await _with_completions(tx, [(found[0], found[1])], allowed)
    return states[0]


async def _with_completions(
    tx: AsyncSession,
    pairs: Iterable[tuple[Todo, str | None]],
    allowed: Mapping[str, Sequence[date]],
) -> list[TodoState]:
    pairs = list(pairs)
    recurring_ids = [row.id for row, _ in pairs if row.kind != "one_off"]
    ticked: dict[uuid.UUID, list[date]] = {}
    if recurring_ids:
        completions = await tx.execute(
            select(TodoCompletion.todo_id, TodoCompletion.period_start).where(
                TodoCompletion.todo_id.in_(recurring_ids)
            )
        )
        for todo_id, period in completions.all():
            ticked.setdefault(todo_id, []).append(period)
    states: list[TodoState] = []
    for row, status in pairs:
        periods: tuple[date, ...] = ()
        if row.kind != "one_off":
            window = set(allowed[row.kind])
            periods = tuple(sorted(p for p in ticked.get(row.id, []) if p in window))
        states.append(
            TodoState(row=row, project_archived=status == "archived", completed_periods=periods)
        )
    return states


async def project_status(tx: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> str | None:
    """The status of the user's own project, or None if unknown or foreign."""
    return (
        await tx.execute(
            select(Project.status).where(Project.id == project_id, Project.user_id == user_id)
        )
    ).scalar_one_or_none()


def _open() -> Any:
    """Open = recurring (always) or a one-off not yet done. Done one-offs leave the manual order."""
    return (Todo.kind != "one_off") | Todo.completed_at.is_(None)


async def open_ids_of_kind(tx: AsyncSession, user_id: uuid.UUID, kind: str) -> list[uuid.UUID]:
    """The ids reorder must cover: every todo of `kind` that is open, in position order."""
    rows = await tx.execute(
        select(Todo.id)
        .where(Todo.user_id == user_id, Todo.kind == kind, _open())
        .order_by(Todo.position, Todo.created_at, Todo.id)
    )
    return list(rows.scalars())


async def next_open_one_off_position(tx: AsyncSession, user_id: uuid.UUID) -> int:
    """max(position of open one-offs) + 1, or 0 when none are open."""
    last = (
        await tx.execute(
            select(func.max(Todo.position)).where(
                Todo.user_id == user_id, Todo.kind == "one_off", _open()
            )
        )
    ).scalar_one()
    return 0 if last is None else last + 1


async def create(
    tx: AsyncSession,
    user_id: uuid.UUID,
    *,
    kind: str,
    title: str,
    notes: str | None,
    due_date: date | None,
    project_id: uuid.UUID | None,
    now: datetime,
) -> uuid.UUID:
    """Append to the end of the kind's manual order (max position + 1, or 0)."""
    last = (
        await tx.execute(
            select(func.max(Todo.position)).where(Todo.user_id == user_id, Todo.kind == kind)
        )
    ).scalar_one()
    todo_id = uuid.uuid4()
    tx.add(
        Todo(
            id=todo_id,
            user_id=user_id,
            kind=kind,
            title=title,
            notes=notes,
            due_date=due_date,
            project_id=project_id,
            position=0 if last is None else last + 1,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    await tx.flush()
    return todo_id


async def update_todo(
    tx: AsyncSession,
    user_id: uuid.UUID,
    todo_id: uuid.UUID,
    values: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    """Set the given columns on the user's todo. Caller has already applied the rules."""
    await tx.execute(
        update(Todo)
        .where(Todo.id == todo_id, Todo.user_id == user_id)
        .values(**values, updated_at=now)
        .execution_options(synchronize_session=False)
    )


async def delete_todo(tx: AsyncSession, user_id: uuid.UUID, todo_id: uuid.UUID) -> bool:
    """Hard delete; completions cascade in the DB. False if the todo is not the user's."""
    result = await tx.execute(delete(Todo).where(Todo.id == todo_id, Todo.user_id == user_id))
    return result.rowcount == 1


async def tick(tx: AsyncSession, todo_id: uuid.UUID, period_start: date, now: datetime) -> None:
    """Idempotent: an existing completion for the period is left untouched."""
    await tx.execute(
        insert(TodoCompletion)
        .values(todo_id=todo_id, period_start=period_start, completed_at=now)
        .on_conflict_do_nothing(index_elements=["todo_id", "period_start"])
    )


async def untick(tx: AsyncSession, todo_id: uuid.UUID, period_start: date) -> None:
    await tx.execute(
        delete(TodoCompletion).where(
            TodoCompletion.todo_id == todo_id, TodoCompletion.period_start == period_start
        )
    )


async def rewrite_positions(
    tx: AsyncSession, user_id: uuid.UUID, ids: Sequence[uuid.UUID], *, now: datetime
) -> None:
    """Positions become 0..n-1 in the order given. The caller has verified `ids` is the kind's
    full id set; this runs inside the caller's transaction."""
    for position, todo_id in enumerate(ids):
        await tx.execute(
            update(Todo)
            .where(Todo.id == todo_id, Todo.user_id == user_id)
            .values(position=position, updated_at=now)
            .execution_options(synchronize_session=False)
        )

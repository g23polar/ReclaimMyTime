"""Todos fixtures shared by the contract cases (tests/api/test_contract.py) and the behavioural
tests (tests/api/test_todos.py). The ids and the story match contracts/fixtures/*todos*.json.

Writes here go straight to the ORM: seeding is not part of the API under test."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Todo, TodoCompletion

Maker = async_sessionmaker[AsyncSession]

# Fixture clock: Tue 2026-11-24 10:00 in America/Los_Angeles; the week starts Monday.
STORY_NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)
STORY_TZ = "America/Los_Angeles"

STRETCH = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000001")  # daily, ticked today
REVIEW = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000002")  # weekly
PASSPORT = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000003")  # one_off
LIBRARY = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000004")  # one_off, done 2026-11-22

_CREATED = datetime(2026, 11, 15, 3, 10, tzinfo=UTC)


async def add_todo(maker: Maker, uid: uuid.UUID, **fields: Any) -> uuid.UUID:
    todo_id = fields.pop("id", None) or uuid.uuid4()
    values: dict[str, Any] = {
        "kind": "one_off",
        "title": "Task",
        "notes": None,
        "due_date": None,
        "position": 0,
        "completed_at": None,
        "created_at": _CREATED,
        "updated_at": _CREATED,
    }
    values |= fields
    async with maker() as s, s.begin():
        s.add(Todo(id=todo_id, user_id=uid, **values))
    return todo_id


async def tick(maker: Maker, todo_id: uuid.UUID, period: date) -> None:
    async with maker() as s, s.begin():
        s.add(TodoCompletion(todo_id=todo_id, period_start=period, completed_at=_CREATED))


async def seed_story(maker: Maker, uid: uuid.UUID) -> None:
    """The get_todos / reorder fixture story: a daily (ticked today), a weekly, an open one-off
    and a one-off done two days ago."""
    await add_todo(maker, uid, id=STRETCH, kind="daily", title="Stretch", position=0)
    await add_todo(
        maker,
        uid,
        id=REVIEW,
        kind="weekly",
        title="Review budget",
        notes="Bank export first.",
        position=0,
    )
    await add_todo(
        maker,
        uid,
        id=PASSPORT,
        title="Renew passport",
        due_date=date(2026, 12, 10),
        position=0,
    )
    await add_todo(
        maker,
        uid,
        id=LIBRARY,
        title="Return library books",
        position=1,
        completed_at=datetime(2026, 11, 22, 18, 30, tzinfo=UTC),
    )
    await tick(maker, STRETCH, date(2026, 11, 24))


async def count(maker: Maker, model: Any, *where: Any) -> int:
    async with maker() as s:
        query = select(func.count()).select_from(model).where(*where)
        return int((await s.execute(query)).scalar_one())

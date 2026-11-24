"""Todos fixtures shared by the contract cases (tests/api/test_contract.py) and the behavioural
tests (tests/api/test_todos.py). The ids and the story match contracts/fixtures/*todos*.json.

Writes here go straight to the ORM: seeding is not part of the API under test."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Project, Todo, TodoCompletion

Maker = async_sessionmaker[AsyncSession]

# Fixture clock: Tue 2026-11-24 10:00 in America/Los_Angeles; the week starts Monday.
STORY_NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)
STORY_TZ = "America/Los_Angeles"

STRETCH = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000001")  # daily, ticked today
REVIEW = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000002")  # weekly
PASSPORT = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000003")  # one_off, linked to PORTFOLIO
FORM = uuid.UUID("7e1c0a52-1d3b-4c1e-9a10-000000000004")  # one_off, second in order
PORTFOLIO = uuid.UUID("5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01")  # active project
STAGED = uuid.UUID("9d4e2b18-6c3a-4f70-8b21-0a7e5c1d3f09")  # staged: cannot be linked

_CREATED = datetime(2026, 11, 15, 3, 10, tzinfo=UTC)


async def add_project(
    maker: Maker,
    uid: uuid.UUID,
    *,
    status: str = "active",
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    pid = project_id or uuid.uuid4()
    async with maker() as s, s.begin():
        s.add(
            Project(
                id=pid,
                user_id=uid,
                name=f"Project {status}",
                kind="perennial",
                weekly_target_hours=Decimal("3.0"),
                max_block_minutes=60,
                min_block_minutes=25,
                status=status,
            )
        )
    return pid


async def add_todo(maker: Maker, uid: uuid.UUID, **fields: Any) -> uuid.UUID:
    todo_id = fields.pop("id", None) or uuid.uuid4()
    values: dict[str, Any] = {
        "kind": "one_off",
        "title": "Task",
        "notes": None,
        "due_date": None,
        "project_id": None,
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
    """The fixture story: one daily (ticked today), one weekly, two one-offs (the first linked to
    an active project), and a staged project."""
    await add_project(maker, uid, project_id=PORTFOLIO)
    await add_project(maker, uid, status="staged", project_id=STAGED)
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
        project_id=PORTFOLIO,
        position=0,
    )
    await add_todo(maker, uid, id=FORM, title="Sign the form", position=1)
    await tick(maker, STRETCH, date(2026, 11, 24))


async def count(maker: Maker, model: Any, *where: Any) -> int:
    async with maker() as s:
        query = select(func.count()).select_from(model).where(*where)
        return int((await s.execute(query)).scalar_one())
